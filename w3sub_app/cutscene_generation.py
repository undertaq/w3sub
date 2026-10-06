"""Discover bundled cutscenes and stage verified overrides outside the game."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
import struct
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile

from .bundle_reader import (
    BundleEntry, BundleReadError, enumerate_witcher_bundles, iter_bundle_entries, write_bundle_entry,
    _HEADER_SIZE, _ENTRY_SIZE, _SIGNATURE, _read_exact,
)
from .cutscene_subs import merge_subs
from .models import CutsceneGenerationSummary, GameInstallation, ResourceFingerprint
from .progress import ProgressCallback, ProgressUpdate, report_progress
from .usm_subtitles import USM_LOCALE_IDS, USMReadError, patch_usm_stream


@dataclass(frozen=True)
class CutsceneBuildResult:
    output_files: dict[str, Path]
    output_hashes: dict[str, str]
    bundle_fingerprint: ResourceFingerprint
    summary: CutsceneGenerationSummary
    unmatched_rows: tuple[tuple[str, ...], ...]
    bundle_content_fingerprint: ResourceFingerprint


class CutsceneGenerationError(ValueError):
    """Cutscene inputs changed or cannot be staged safely."""


_INVENTORY_CACHE: dict[tuple[str, object, str], tuple[BundleEntry, ...]] = {}
_SIDECAR_LIMIT = 16 * 1024 * 1024


def safe_relative_path(raw: str) -> str:
    """Normalize separators while rejecting Windows aliases and traversal."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("resource path must be a nonempty string")
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    windows = PureWindowsPath(raw)
    if windows.drive or windows.is_absolute() or PurePosixPath(normalized).is_absolute():
        raise ValueError(f"absolute resource path: {raw!r}")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((" ", "."))
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
                       or char in '<>:"|?*' for char in part)
                or part.split(".", 1)[0].casefold() in {
                    "con", "prn", "aux", "nul", "conin$", "conout$",
                    *(f"com{number}" for number in range(1, 10)),
                    *(f"lpt{number}" for number in range(1, 10)),
                    *(f"{prefix}{number}" for prefix in ("com", "lpt") for number in "¹²³"),
                }):
            raise ValueError(f"unsafe resource path: {raw!r}")
    return "/".join(parts).casefold()


def fingerprint_cutscene_bundles(game: GameInstallation) -> ResourceFingerprint:
    """Fast identity: all active bundle paths, physical sizes and mtime_ns.

    Including bundles without cutscenes detects new/deleted source bundles
    without reading or decompressing any entry during freshness checks.
    """
    root = Path(game.root).resolve(strict=True)
    entries: dict[str, str] = {}
    for bundle in enumerate_witcher_bundles(root):
        resolved = bundle.resolve(strict=True)
        try:
            relative = safe_relative_path(resolved.relative_to(root).as_posix())
        except ValueError as error:
            raise CutsceneGenerationError(f"Unsafe source bundle: {bundle}") from error
        if relative in entries:
            raise CutsceneGenerationError(f"Duplicate source bundle path: {relative}")
        stat = resolved.stat()
        metadata = f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")
        entries[relative] = hashlib.sha256(metadata).hexdigest()
    aggregate = hashlib.sha256()
    for relative, digest in sorted(entries.items()):
        encoded = relative.encode("utf-8")
        aggregate.update(len(encoded).to_bytes(8, "big"))
        aggregate.update(encoded)
        aggregate.update(bytes.fromhex(digest))
    return ResourceFingerprint(entries, aggregate.hexdigest())


def _aggregate_fingerprint(entries: dict[str, str]) -> ResourceFingerprint:
    aggregate = hashlib.sha256()
    for relative, digest in sorted(entries.items()):
        encoded = relative.encode("utf-8")
        aggregate.update(len(encoded).to_bytes(8, "big"))
        aggregate.update(encoded)
        aggregate.update(bytes.fromhex(digest))
    return ResourceFingerprint(entries, aggregate.hexdigest())


def _bundle_file_identity(info) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _fingerprint_bundle_tables(
    game: GameInstallation, metadata: ResourceFingerprint,
    callback: ProgressCallback | None, phase: str,
) -> tuple[ResourceFingerprint, tuple[str, ...], dict[str, tuple[int, ...]]]:
    """Hash live v5 headers/tables and select full-content scope from their names."""
    root = Path(game.root).resolve(strict=True)
    plans = {}
    total = 0
    for relative in sorted(metadata.entries):
        path = root.joinpath(*PurePosixPath(relative).parts)
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CutsceneGenerationError(f"Source bundle path is redirected: {relative}")
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            header = _read_exact(source, _HEADER_SIZE, path, "bundle header")
        declared = struct.unpack_from("<Q", header, 8)[0]
        size = struct.unpack_from("<I", header, 16)[0]
        version = struct.unpack_from("<H", header, 20)[0]
        if (header[:8] != _SIGNATURE or version != 5 or declared != info.st_size
                or size % _ENTRY_SIZE or _HEADER_SIZE + size > info.st_size):
            raise BundleReadError(path, "invalid v5 header/table bounds during content fingerprinting")
        plans[relative] = (path, header, size, _bundle_file_identity(info))
        total += _HEADER_SIZE + size
    entries = {}
    relevant = []
    identities = {}
    completed = 0
    report_progress(callback, phase, 0, total)
    block_size = (1024 * 1024 // _ENTRY_SIZE) * _ENTRY_SIZE
    for relative, (path, expected_header, size, identity) in plans.items():
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if _bundle_file_identity(before) != identity:
                raise CutsceneGenerationError(f"Source bundle changed before table hashing: {relative}")
            header = _read_exact(source, _HEADER_SIZE, path, "bundle header")
            if header != expected_header:
                raise CutsceneGenerationError(f"Source bundle header changed while hashing: {relative}")
            digest = hashlib.sha256(b"w3sub-table-v1\0" + header)
            completed += len(header)
            report_progress(callback, phase, completed, total)
            remaining = size
            has_cutscenes = False
            while remaining:
                block = _read_exact(source, min(remaining, block_size), path, "bundle metadata table")
                digest.update(block)
                for offset in range(0, len(block), _ENTRY_SIZE):
                    name = block[offset:offset + 256].split(b"\0", 1)[0].lower()
                    if name.endswith((b".subs", b".usm")):
                        has_cutscenes = True
                remaining -= len(block)
                completed += len(block)
                report_progress(callback, phase, completed, total)
            after = os.fstat(source.fileno())
        if identity != _bundle_file_identity(after) or identity != _bundle_file_identity(path.stat()):
            raise CutsceneGenerationError(f"Source bundle changed while table hashing: {relative}")
        entries[relative] = digest.hexdigest()
        identities[relative] = identity
        if has_cutscenes:
            relevant.append(relative)
    return _aggregate_fingerprint(entries), tuple(relevant), identities


def fingerprint_cutscene_bundle_contents(
    game: GameInstallation, *, progress_callback: ProgressCallback | None = None,
    phase: str = "Hashing cutscene source bundles",
) -> ResourceFingerprint:
    """Hash all live header/tables and full bytes of candidate-containing bundles.

    The per-bundle digest includes a table/content domain tag. Recomputing scope
    from every table detects hidden new candidates even with unchanged size and
    timestamps. Startup uses only the separate metadata fingerprint helper.
    """
    metadata = fingerprint_cutscene_bundles(game)
    root = Path(game.root).resolve(strict=True)
    tables, relevant, identities = _fingerprint_bundle_tables(
        game, metadata, progress_callback, phase + ": metadata tables")
    entries = dict(tables.entries)
    total = sum(identities[relative][2] for relative in relevant)
    completed = 0
    report_progress(progress_callback, phase + ": relevant bundle contents", 0, total)
    for relative in relevant:
        path = root.joinpath(*PurePosixPath(relative).parts)
        digest = hashlib.sha256(b"w3sub-content-v1\0")
        with path.open("rb") as source:
            if _bundle_file_identity(os.fstat(source.fileno())) != identities[relative]:
                raise CutsceneGenerationError(f"Source bundle changed before content hashing: {relative}")
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                completed += len(block)
                report_progress(progress_callback, phase + ": relevant bundle contents", completed, total)
            after = os.fstat(source.fileno())
        if (identities[relative] != _bundle_file_identity(after)
                or identities[relative] != _bundle_file_identity(path.stat())):
            raise CutsceneGenerationError(f"Source bundle changed while content hashing: {relative}")
        entries[relative] = digest.hexdigest()
    # Tables are small; repeat them after the potentially long relevant pass.
    final_tables, final_relevant, _ = _fingerprint_bundle_tables(
        game, metadata, progress_callback, phase + ": rechecking metadata tables")
    if final_tables != tables or final_relevant != relevant or fingerprint_cutscene_bundles(game) != metadata:
        raise CutsceneGenerationError("Source bundle inventory/tables changed while hashing; retry")
    return _aggregate_fingerprint(entries)


def _inventory(game: GameInstallation, fingerprint: ResourceFingerprint,
               callback: ProgressCallback | None,
               content_fingerprint: ResourceFingerprint) -> tuple[BundleEntry, ...]:
    root = Path(game.root).resolve(strict=True)
    key = (str(root), game.version, fingerprint.digest + content_fingerprint.digest)
    cached = _INVENTORY_CACHE.get(key)
    if cached is not None:
        report_progress(callback, "Using cached cutscene bundle inventory", len(cached), None)
        return cached
    bundles = enumerate_witcher_bundles(root)
    selected = []
    for number, bundle in enumerate(bundles, 1):
        for entry in iter_bundle_entries(bundle):
            if PurePosixPath(entry.depot_path.replace("\\", "/")).suffix.casefold() in (".subs", ".usm"):
                selected.append(entry)
        report_progress(callback, "Scanning cutscene bundle tables", number, len(bundles))
    if fingerprint_cutscene_bundles(game) != fingerprint:
        raise CutsceneGenerationError("Source bundles changed during cutscene discovery; retry")
    # Keep only the most recent inventory. Large metadata collections must not
    # accumulate as the user changes installations or updates the game.
    _INVENTORY_CACHE.clear()
    result = tuple(selected)
    _INVENTORY_CACHE[key] = result
    return result


def _phase(callback: ProgressCallback | None, label: str) -> ProgressCallback:
    def forward(update: ProgressUpdate) -> None:
        report_progress(callback, f"{label}: {update.phase}", update.completed, update.total)
    return forward


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _aliases(path: str) -> tuple[str, ...]:
    parts = path.split("/")
    aliases = {path}
    # Playback aliases are directory names, never arbitrary text in a filename.
    for index, part in enumerate(parts[:-1]):
        if part in ("subs", "altsubs"):
            alternate = list(parts)
            alternate[index] = "altsubs" if part == "subs" else "subs"
            aliases.add("/".join(alternate))
    return tuple(sorted(aliases))


def _sidecar_key(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    stem = PurePosixPath(parts[-1]).stem
    if "_" not in stem:
        return None
    resource, locale = stem.rsplit("_", 1)
    if not resource or not locale:
        return None
    parent = ["subs" if part == "altsubs" else part for part in parts[:-1]]
    return "/".join(parent + [resource]), locale


def build_cutscene_overrides(
    game: GameInstallation, output_root: Path, primary_language: str, secondary_language: str,
    *, progress_callback: ProgressCallback | None = None,
    bundle_content_fingerprint: ResourceFingerprint | None = None,
) -> CutsceneBuildResult:
    """Stage changed resources only; remove all created outputs on failure.

    Bundle verification errors propagate and abort the entire generation.
    Format/layout refusal instead records the resource and skips its override.
    """
    primary_language, secondary_language = primary_language.casefold(), secondary_language.casefold()
    if not primary_language or not secondary_language or primary_language == secondary_language:
        raise CutsceneGenerationError("Choose two different cutscene languages")
    root = Path(output_root).resolve()
    game_root = Path(game.root).resolve(strict=True)
    try:
        root.relative_to(game_root)
    except ValueError:
        pass
    else:
        raise CutsceneGenerationError("Cutscene staging must be outside the game folder")
    root.mkdir(parents=True, exist_ok=True)
    fingerprint = fingerprint_cutscene_bundles(game)
    content_fingerprint = (bundle_content_fingerprint
                           if bundle_content_fingerprint is not None else
                           fingerprint_cutscene_bundle_contents(game, progress_callback=progress_callback))
    if content_fingerprint.entries.keys() != fingerprint.entries.keys():
        raise CutsceneGenerationError("Source bundle inventory changed before cutscene generation; retry")
    inventory = _inventory(game, fingerprint, progress_callback, content_fingerprint)
    rows: list[tuple[str, ...]] = []
    outputs: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    counts = {name: 0 for name in CutsceneGenerationSummary.__dataclass_fields__}

    def skipped(path: str, locale: str, reason: str) -> None:
        # Bundle names may contain undecodable bytes. Keep their diagnostics
        # printable so one malformed name cannot break the UTF-8 CSV report.
        rows.append((path.encode("utf-8", "backslashreplace").decode("utf-8"),
                     locale, "", "", "",
                     reason.encode("utf-8", "backslashreplace").decode("utf-8")))

    # Duplicate virtual paths must be equivalent. Never guess bundle priority
    # when two physical copies advertise different content.
    candidates: dict[str, list[BundleEntry]] = {}
    for entry in inventory:
        try:
            path = safe_relative_path(entry.depot_path)
        except ValueError as error:
            skipped(entry.depot_path, "", str(error))
            kind = "usm" if entry.depot_path.casefold().endswith(".usm") else "sidecar"
            # Unsafe paths never enter the valid resource groups. Count their
            # discovery here alongside their skip, exactly once per entry.
            counts[f"{kind}_discovered"] += 1
            counts[f"{kind}_skipped"] += 1
            continue
        candidates.setdefault(path, []).append(entry)
    resources: dict[str, BundleEntry] = {}
    ambiguous: set[str] = set()
    for path, entries in candidates.items():
        identities = {(entry.uncompressed_size, entry.crc32) for entry in entries}
        if len(entries) > 1 and (len(identities) != 1 or entries[0].crc32 == 0):
            ambiguous.add(path)
        else:
            resources[path] = entries[0]

    sidecars: dict[str, dict[str, list[str]]] = {}
    for path in sorted(candidates):
        if path.endswith(".subs"):
            key = _sidecar_key(path)
            if key is None:
                # Key-less paths also cannot contribute a logical stem.
                counts["sidecar_discovered"] += 1
                counts["sidecar_skipped"] += 1
                skipped(path, "", "sidecar has no resource stem and locale suffix")
            elif key[1] in (primary_language, secondary_language):
                sidecars.setdefault(key[0], {}).setdefault(key[1], []).append(path)
    movies = sorted(path for path in candidates if path.endswith(".usm"))
    # Valid groups are disjoint from the malformed discoveries counted above.
    # Later skips are already represented by these groups' discovery totals.
    counts["sidecar_discovered"] += len(sidecars)
    counts["usm_discovered"] += len(movies)
    counts["estimated_work_bytes"] = sum(entry.uncompressed_size for entry in resources.values()
                                         if entry.depot_path.casefold().endswith(".usm")
                                         or (_sidecar_key(safe_relative_path(entry.depot_path)) or ("", ""))[1]
                                         in (primary_language, secondary_language))
    counts["estimated_output_bytes"] = sum(resources[path].uncompressed_size for path in movies
                                           if path in resources)
    for locales in sidecars.values():
        paths = locales.get(primary_language, [])
        if paths and paths[0] in resources:
            # UTF-16 text may grow to primary + secondary, with both aliases.
            primary_size = resources[paths[0]].uncompressed_size
            secondary_paths = locales.get(secondary_language, [])
            secondary_size = (resources[secondary_paths[0]].uncompressed_size
                              if secondary_paths and secondary_paths[0] in resources else 0)
            counts["estimated_output_bytes"] += (primary_size + secondary_size) * len(_aliases(paths[0]))
    report_progress(progress_callback, "Estimated cutscene extraction bytes", 0,
                    counts["estimated_work_bytes"] or None)

    def publish(path: str, staged: Path) -> None:
        normalized = safe_relative_path(path)
        target = root.joinpath(*normalized.split("/"))
        if not target.resolve().is_relative_to(root) or target.exists():
            raise CutsceneGenerationError(f"Unsafe or occupied cutscene output: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(root) or target.exists():
            raise CutsceneGenerationError(f"Unsafe or occupied cutscene output: {path}")
        digest = _hash(staged)
        os.replace(staged, target)
        outputs[normalized] = target
        hashes[normalized] = digest
        counts["output_bytes"] += target.stat().st_size

    def add_cues(path: str, result) -> None:
        counts["primary_cues"] += result.primary_count
        counts["secondary_cues"] += result.secondary_count
        counts["matched_cues"] += result.matched_count
        counts["unmatched_count"] += len(result.unmatched_primary) + len(result.unmatched_secondary)
        for locale, cues, reason in (
            (primary_language, result.unmatched_primary, "no exact secondary timing match"),
            (secondary_language, result.unmatched_secondary, "no exact primary timing anchor"),
        ):
            for cue in cues:
                rows.append((path, locale, str(cue.start), str(cue.end), cue.text, reason))

    def sidecar_bytes(path: str) -> bytes:
        entry = resources[path]
        if entry.uncompressed_size > _SIDECAR_LIMIT:
            raise ValueError(f"sidecar exceeds {_SIDECAR_LIMIT} byte memory limit")
        destination = io.BytesIO()
        write_bundle_entry(entry.bundle_path, entry, destination,
                           _phase(progress_callback, f"Extracting sidecar {path}"))
        return destination.getvalue()

    try:
        with tempfile.TemporaryDirectory(prefix=".cutscene-", dir=root.parent) as temporary:
            scratch = Path(temporary)
            for number, (stem, locales) in enumerate(sorted(sidecars.items()), 1):
                report_progress(progress_callback, "Merging cutscene sidecar resources", number, len(sidecars))
                selected = locales.get(primary_language, []) + locales.get(secondary_language, [])
                if any(path in ambiguous for path in selected):
                    counts["sidecar_skipped"] += 1
                    skipped(stem, "", "conflicting or unverifiable duplicate sidecar paths")
                    continue
                if primary_language not in locales or secondary_language not in locales:
                    counts["sidecar_skipped"] += 1
                    missing = primary_language if primary_language not in locales else secondary_language
                    skipped(stem, missing, "selected locale sidecar is absent")
                    continue
                # Alias copies must agree before identical overrides can be emitted.
                primary_paths, secondary_paths = locales[primary_language], locales[secondary_language]
                if any(resources[path].uncompressed_size > _SIDECAR_LIMIT for path in selected):
                    counts["sidecar_skipped"] += 1
                    skipped(stem, "", f"sidecar exceeds {_SIDECAR_LIMIT} byte memory limit")
                    continue
                primary_data = [sidecar_bytes(path) for path in primary_paths]
                secondary_data = [sidecar_bytes(path) for path in secondary_paths]
                if len(set(primary_data)) != 1 or len(set(secondary_data)) != 1:
                    counts["sidecar_skipped"] += 1
                    skipped(stem, "", "subs/altsubs source texts disagree")
                    continue
                try:
                    result = merge_subs(primary_data[0], secondary_data[0])
                except ValueError as error:
                    counts["sidecar_skipped"] += 1
                    skipped(primary_paths[0], "", f"unsupported sidecar: {error}")
                    continue
                add_cues(primary_paths[0], result)
                if result.data == primary_data[0]:
                    counts["sidecar_unchanged"] += 1
                    continue
                counts["sidecar_changed"] += 1
                targets = {alias for path in primary_paths for alias in _aliases(path)}
                for path in sorted(targets):
                    staged = scratch / "sidecar-output"
                    staged.write_bytes(result.data)
                    publish(path, staged)

            for number, path in enumerate(movies, 1):
                report_progress(progress_callback, "Processing cutscene USM resources", number, len(movies))
                if path in ambiguous:
                    counts["usm_skipped"] += 1
                    skipped(path, "", "conflicting or unverifiable duplicate USM paths")
                    continue
                absent_ids = [locale for locale in (primary_language, secondary_language)
                              if locale not in USM_LOCALE_IDS]
                if absent_ids:
                    counts["usm_skipped"] += 1
                    for locale in absent_ids:
                        skipped(path, locale, "selected locale has no confirmed USM locale ID")
                    continue
                source_path, staged = scratch / "source.usm", scratch / "output.usm"
                try:
                    entry = resources[path]
                    with source_path.open("wb") as destination:
                        write_bundle_entry(entry.bundle_path, entry, destination,
                                           _phase(progress_callback, f"Extracting cutscene USM {path}"))
                    try:
                        with source_path.open("rb") as source, staged.open("wb") as destination:
                            result = patch_usm_stream(source, destination,
                                                      USM_LOCALE_IDS[primary_language],
                                                      USM_LOCALE_IDS[secondary_language],
                                                      _phase(progress_callback, f"Patching cutscene USM {path}"))
                    except USMReadError as error:
                        counts["usm_skipped"] += 1
                        skipped(path, "", f"unsupported USM: {error}")
                        continue
                    add_cues(path, result)
                    if not result.primary_count or not result.secondary_count:
                        counts["usm_skipped"] += 1
                        for locale, count in ((primary_language, result.primary_count),
                                              (secondary_language, result.secondary_count)):
                            if not count:
                                skipped(path, locale, "selected USM locale has no subtitle cues")
                    elif result.changed:
                        if staged.stat().st_size != result.bytes_written:
                            raise CutsceneGenerationError(f"USM staged byte count disagrees: {path}")
                        publish(path, staged)
                        counts["usm_changed"] += 1
                    else:
                        counts["usm_unchanged"] += 1
                finally:
                    source_path.unlink(missing_ok=True)
                    staged.unlink(missing_ok=True)
        report_progress(progress_callback, "Rechecking cutscene bundle metadata", 0, 1)
        if fingerprint_cutscene_bundles(game) != fingerprint:
            raise CutsceneGenerationError("Source bundles changed during cutscene generation; retry")
        report_progress(progress_callback, "Rechecking cutscene bundle metadata", 1, 1)
        if fingerprint_cutscene_bundle_contents(
                game, progress_callback=progress_callback, phase="Rechecking cutscene bundle contents") != content_fingerprint:
            raise CutsceneGenerationError("Source bundle contents changed during cutscene generation; retry")
        return CutsceneBuildResult(outputs, hashes, fingerprint,
                                   CutsceneGenerationSummary(**counts), tuple(rows), content_fingerprint)
    except BaseException:
        for target in outputs.values():
            target.unlink(missing_ok=True)
        raise
