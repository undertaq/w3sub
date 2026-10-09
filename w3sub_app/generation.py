"""Stage dual-language resources and retain the inputs needed to audit them."""
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import uuid
import shutil
from dataclasses import asdict

from .merge import (
    MergeError,
    MergeStats,
    merge_csv,
    merge_records,
    unmatched_csv_entries,
    unmatched_native_entries,
)
from .converter import check_compatibility
from .cutscene_generation import (
    build_cutscene_overrides, fingerprint_cutscene_bundles,
    fingerprint_cutscene_bundle_contents, movie_companion_path, safe_relative_path,
)
from .movie_mods import (
    BACKEND_ID as MOVIE_PACKAGE_BACKEND,
    BACKEND_VERSION as MOVIE_PACKAGE_BACKEND_VERSION,
    build_movie_packages,
    validate_movie_package,
)
from .progress import ProgressCallback, report_progress
from .w3strings_native import NativeW3StringsCodec
from .models import (
    CutsceneGenerationSummary,
    Freshness,
    GameInstallation,
    GameVersion,
    GenerationRecord,
    GenerationRequest,
    MergeMode,
    ResourceFingerprint,
    Storefront,
)


APP_VERSION = "0.1.12"
GENERATION_RECORD_SCHEMA = 2


class GenerationError(RuntimeError):
    """A generation could not be produced safely."""


def _normalized_game_root(root: Path) -> Path:
    return Path(root).expanduser().resolve()


def _game_root_hash(root: Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(_normalized_game_root(root))))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _relative_resource(root: Path, path: Path) -> str:
    candidate = Path(path)
    absolute = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError as error:
        raise GenerationError(f"Language resource is outside the game folder: {path}") from error


def _resource_map(game: GameInstallation, language: str) -> dict[str, Path]:
    root = _normalized_game_root(game.root)
    paths = game.language_files.get(language)
    if not paths:
        raise GenerationError(f"No .w3strings resources are available for {language!r}")
    resources = {}
    for raw_path in paths:
        path = Path(raw_path)
        relative = _relative_resource(root, path)
        if Path(relative).suffix.casefold() != ".w3strings":
            raise GenerationError(f"Not a .w3strings resource: {relative}")
        if Path(relative).stem.casefold() != language.casefold():
            raise GenerationError(
                f"Resource language does not match {language!r}: {relative}"
            )
        if relative in resources:
            raise GenerationError(f"Duplicate language resource path: {relative}")
        resources[relative] = path if path.is_absolute() else root / path
    return resources


def _pair_resources(game: GameInstallation, primary_language: str,
                    secondary_language: str) -> list[tuple[str, str, str]]:
    primary = _resource_map(game, primary_language)
    secondary = _resource_map(game, secondary_language)
    # The localized filename differs, so pair by the game-relative resource
    # directory, which is the stable path portion shared across locales.
    primary_by_parent = {PurePosixPath(path).parent.as_posix(): path for path in primary}
    secondary_by_parent = {PurePosixPath(path).parent.as_posix(): path for path in secondary}
    if len(primary_by_parent) != len(primary) or len(secondary_by_parent) != len(secondary):
        raise GenerationError("More than one language resource occupies a resource directory")
    if primary_by_parent.keys() != secondary_by_parent.keys():
        missing_secondary = sorted(primary_by_parent.keys() - secondary_by_parent.keys())
        missing_primary = sorted(secondary_by_parent.keys() - primary_by_parent.keys())
        raise GenerationError(
            "Language resource inventories do not pair by relative directory "
            f"(missing {secondary_language}: {missing_secondary}; "
            f"missing {primary_language}: {missing_primary})"
        )
    return [
        (parent, primary_by_parent[parent], secondary_by_parent[parent])
        for parent in sorted(primary_by_parent)
    ]


def _source_paths(game: GameInstallation, primary_language: str,
                  secondary_language: str) -> dict[str, Path]:
    root = _normalized_game_root(game.root)
    pairs = _pair_resources(game, primary_language, secondary_language)
    primary_resources = _resource_map(game, primary_language)
    secondary_resources = _resource_map(game, secondary_language)
    sources = {}
    for _, primary_rel, secondary_rel in pairs:
        sources[primary_rel] = primary_resources[primary_rel]
        sources[secondary_rel] = secondary_resources[secondary_rel]

    return sources


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint_sources(sources: dict[str, Path],
                         progress_callback: ProgressCallback | None = None,
                         phase: str = "Fingerprinting resources") -> ResourceFingerprint:
    entries = {}
    aggregate = hashlib.sha256()
    if sources:
        report_progress(progress_callback, phase, 0, len(sources))
    for number, (relative, path) in enumerate(sorted(sources.items()), start=1):
        digest = _hash_file(path)
        entries[relative] = digest
        encoded = relative.encode("utf-8")
        aggregate.update(len(encoded).to_bytes(8, "big"))
        aggregate.update(encoded)
        aggregate.update(bytes.fromhex(digest))
        report_progress(progress_callback, phase, number, len(sources))
    return ResourceFingerprint(entries, aggregate.hexdigest())


def _hash_executable(converter) -> tuple[Path, str]:
    executable = getattr(converter, "executable", None)
    if executable is None:
        raise GenerationError("Converter does not expose its executable path")
    try:
        resolved = Path(executable).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(f"Converter is not a file: {resolved}")
        return resolved, _hash_file(resolved)
    except (OSError, TypeError) as error:
        raise GenerationError(f"Cannot identify converter executable: {error}") from error


def _version_payload(version: GameVersion) -> dict[str, str | None]:
    return {
        "executable_version": version.executable_version,
        "executable_version_raw": version.executable_version_raw,
        "store_build_id": version.store_build_id,
    }


def _fingerprint_payload(fingerprint: ResourceFingerprint) -> dict[str, object]:
    return {"entries": dict(sorted(fingerprint.entries.items())), "digest": fingerprint.digest}


def _write_unmatched_report(path: Path, rows, *,
                            columns: tuple[str, ...] = (
                                "resource", "language", "side", "string_id",
                                "key_hash_hex", "text", "reason")) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="", prefix=".unmatched-",
                suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, csv.Error, UnicodeError) as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise GenerationError(f"Cannot write unmatched entries report: {error}") from error


def _record_payload(record: GenerationRecord) -> dict[str, object]:
    return {
        "schema_version": GENERATION_RECORD_SCHEMA,
        "codec_kind": record.codec_kind,
        "generation_id": record.generation_id,
        "game_root": str(record.game_root),
        "generation_dir": str(record.generation_dir),
        "storefront": record.storefront.value,
        "game_version": _version_payload(record.game_version),
        "primary_language": record.primary_language,
        "secondary_language": record.secondary_language,
        "mode": record.mode.value,
        "source_fingerprint": _fingerprint_payload(record.source_fingerprint),
        "classifier_digest": record.classifier_digest,
        "classifier_schema_version": record.classifier_schema_version,
        "converter_path": record.converter_path,
        "converter_sha256": record.converter_sha256,
        "converter_version": record.converter_version,
        "app_version": record.app_version,
        "output_files": dict(sorted(record.output_files.items())),
        "output_hashes": dict(sorted(record.output_hashes.items())),
        "total_entries": record.total_entries,
        "merged_entries": record.merged_entries,
        "unmatched_entries_count": record.unmatched_entries_count,
        "include_cutscenes": record.include_cutscenes,
        "cutscene_output_files": dict(sorted(record.cutscene_output_files.items())),
        "cutscene_output_hashes": dict(sorted(record.cutscene_output_hashes.items())),
        "cutscene_source_bundles": {
            key: list(value) for key, value in sorted(record.cutscene_source_bundles.items())
        },
        "cutscene_package_files": dict(sorted(record.cutscene_package_files.items())),
        "cutscene_package_hashes": dict(sorted(record.cutscene_package_hashes.items())),
        "cutscene_package_sizes": dict(sorted(record.cutscene_package_sizes.items())),
        "cutscene_package_backend": record.cutscene_package_backend,
        "cutscene_package_backend_version": record.cutscene_package_backend_version,
        "cutscene_summary": asdict(record.cutscene_summary),
        "cutscene_bundle_content_fingerprint": (
            _fingerprint_payload(record.cutscene_bundle_content_fingerprint)
            if record.cutscene_bundle_content_fingerprint is not None else None),
        "cutscene_bundle_fingerprint": (
            _fingerprint_payload(record.cutscene_bundle_fingerprint)
            if record.cutscene_bundle_fingerprint is not None else None),
    }


def generate(request: GenerationRequest, state_root: Path, converter=None, *,
             progress_callback: ProgressCallback | None = None) -> GenerationRecord:
    """Generate into per-game app state and atomically publish its JSON record."""
    if converter is None:
        converter = NativeW3StringsCodec()
    native = isinstance(converter, NativeW3StringsCodec)
    if not isinstance(request.mode, MergeMode):
        raise GenerationError(f"Unsupported merge mode: {request.mode!r}")
    if type(request.include_cutscenes) is not bool:
        raise GenerationError("The cutscene option must be a boolean")
    if (not isinstance(request.primary_language, str)
            or not isinstance(request.secondary_language, str)):
        raise GenerationError("Language selections must be language codes")
    primary_language = request.primary_language.casefold()
    secondary_language = request.secondary_language.casefold()
    if (not primary_language or not secondary_language
            or primary_language == secondary_language):
        raise GenerationError("Choose two different supported languages")
    root = _normalized_game_root(request.game.root)
    pairs = _pair_resources(request.game, primary_language, secondary_language)
    sources = _source_paths(request.game, primary_language, secondary_language)
    try:
        initial_fingerprint = _fingerprint_sources(sources, progress_callback)
    except (OSError, ValueError) as error:
        raise GenerationError(f"Cannot fingerprint generation inputs: {error}") from error

    classifier_digest = None
    classifier_schema = None

    report_progress(progress_callback, "Identifying codec implementation", 0, 1)
    converter_path, converter_digest = _hash_executable(converter)
    report_progress(progress_callback, "Identifying codec implementation", 1, 1)
    converter_version = getattr(converter, "version", None)
    if converter_version is not None and not isinstance(converter_version, str):
        converter_version = str(converter_version)

    state_root = Path(state_root).expanduser().resolve()
    generations_root = (state_root / "games" / _game_root_hash(root) / "generations")
    try:
        generations_root.resolve().relative_to(root)
    except ValueError:
        pass
    else:
        raise GenerationError("Generation state must be outside the game folder")
    generations_root.mkdir(parents=True, exist_ok=True)
    generation_id = uuid.uuid4().hex
    generation_dir = generations_root / generation_id
    generation_dir.mkdir()
    output_files = {}
    output_hashes = {}
    cutscene_package_files: dict[str, str] = {}
    cutscene_package_hashes: dict[str, str] = {}
    cutscene_package_sizes: dict[str, int] = {}
    cutscene_package_backend = None
    cutscene_package_backend_version = None
    merge_stats = MergeStats()
    unmatched_report_rows = []
    published = False

    try:
        initial_bundle_fingerprint = (fingerprint_cutscene_bundles(request.game)
                                      if request.include_cutscenes else None)
        initial_bundle_content = (fingerprint_cutscene_bundle_contents(
            request.game, progress_callback=progress_callback) if request.include_cutscenes else None)
        with tempfile.TemporaryDirectory(prefix="convert-", dir=generations_root) as scratch_name:
            scratch = Path(scratch_name)
            copied_sources = {}
            if sources:
                report_progress(progress_callback, "Copying generation inputs", 0, len(sources))
            for number, (relative, source) in enumerate(sorted(sources.items())):
                copied = scratch / "inputs" / str(number) / Path(relative).name
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, copied)
                copied_sources[relative] = copied
                report_progress(progress_callback, "Copying generation inputs", number + 1, len(sources))
            report = check_compatibility(
                copied_sources, converter, scratch / "compatibility",
                progress_callback=progress_callback,
            )
            if not report.compatible:
                codec_label = "Native codec" if native else "External converter"
                raise GenerationError(f"{codec_label} compatibility check failed: {report.error}")
            for number, (_resource_parent, primary_relative, secondary_relative) in enumerate(pairs):
                report_progress(progress_callback, "Merging resource pairs", number, len(pairs))
                primary_csv = converter.decode(copied_sources[primary_relative],
                                               scratch / f"{number}-primary")
                secondary_csv = converter.decode(copied_sources[secondary_relative],
                                                 scratch / f"{number}-secondary")
                unmatched = (unmatched_native_entries(primary_csv, secondary_csv) if native else
                             unmatched_csv_entries(primary_csv, secondary_csv))
                for entry in unmatched:
                    language = (primary_language if entry.side == "primary"
                                else secondary_language)
                    resource = (primary_relative if entry.side == "primary"
                                else secondary_relative)
                    unmatched_report_rows.append((
                        resource, language, entry.side, entry.string_id,
                        entry.key_hash_hex, entry.text, entry.reason,
                    ))
                try:
                    merged_csv = (merge_records if native else merge_csv)(
                        primary_csv,
                        secondary_csv,
                        request.mode,
                        stats=merge_stats,
                        primary_language=primary_language,
                        secondary_language=secondary_language,
                    )
                except MergeError as error:
                    raise GenerationError(f"Cannot merge {primary_relative}: {error}") from error
                report_progress(progress_callback, "Merging resource pairs",
                                number + 1, len(pairs))
                report_progress(progress_callback, "Encoding output files", number, len(pairs))
                encoded = converter.encode(merged_csv, scratch / f"{number}-encoded")
                target = generation_dir.joinpath(*PurePosixPath(primary_relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with Path(encoded).open("rb") as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(chunk)
                output_files[primary_relative] = str(target.resolve())
                output_hashes[primary_relative] = _hash_file(target)
                report_progress(progress_callback, "Encoding output files",
                                number + 1, len(pairs))

        cutscenes = None
        if request.include_cutscenes:
            cutscenes = build_cutscene_overrides(
                request.game, generation_dir / "cutscenes", primary_language,
                secondary_language, progress_callback=progress_callback,
                bundle_content_fingerprint=initial_bundle_content,
            )
            if cutscenes.bundle_fingerprint != initial_bundle_fingerprint:
                raise GenerationError("Source bundles changed during generation; retry")
            packages = build_movie_packages(
                cutscenes.output_files,
                cutscenes.source_bundles,
                generation_dir / "movie_packages",
                progress_callback=progress_callback,
            )
            cutscene_package_files = {
                target: str(path.resolve()) for target, path in packages.files.items()
            }
            cutscene_package_hashes = dict(packages.hashes)
            cutscene_package_sizes = {
                target: path.stat().st_size for target, path in packages.files.items()
            }
            cutscene_package_backend = packages.backend_id
            cutscene_package_backend_version = packages.backend_version

        try:
            final_fingerprint = _fingerprint_sources(
                sources, progress_callback, "Rechecking generation inputs",
            )
        except (OSError, ValueError) as error:
            raise GenerationError(f"Cannot recheck generation inputs: {error}") from error
        if final_fingerprint != initial_fingerprint:
            raise GenerationError("A language resource changed during generation; retry")
        report_progress(progress_callback, "Rechecking codec implementation", 0, 1)
        current_converter_path, final_converter_digest = _hash_executable(converter)
        if current_converter_path != converter_path or final_converter_digest != converter_digest:
            raise GenerationError("Converter executable changed during generation; retry")
        report_progress(progress_callback, "Rechecking codec implementation", 1, 1)
        if request.include_cutscenes:
            report_progress(progress_callback, "Rechecking cutscene bundle metadata", 0, 1)
            if fingerprint_cutscene_bundles(request.game) != initial_bundle_fingerprint:
                raise GenerationError("Source bundles changed during generation; retry")
            report_progress(progress_callback, "Rechecking cutscene bundle metadata", 1, 1)

        report_progress(progress_callback, "Publishing generation preview", 0, 1)
        unmatched_report = generation_dir / "unmatched_entries.csv"
        _write_unmatched_report(unmatched_report, unmatched_report_rows)
        _write_unmatched_report(
            generation_dir / "cutscene_unmatched.csv",
            cutscenes.unmatched_rows if cutscenes is not None else (),
            columns=("resource", "locale", "start", "end", "text", "reason"),
        )
        record = GenerationRecord(
            generation_id=generation_id,
            game_root=root,
            generation_dir=generation_dir.resolve(),
            storefront=request.game.storefront,
            game_version=request.game.version,
            primary_language=primary_language,
            secondary_language=secondary_language,
            mode=request.mode,
            source_fingerprint=initial_fingerprint,
            classifier_digest=classifier_digest,
            converter_path=str(converter_path),
            converter_sha256=converter_digest,
            converter_version=converter_version,
            app_version=APP_VERSION,
            output_files=output_files,
            output_hashes=output_hashes,
            codec_kind="native" if native else "external",
            classifier_schema_version=classifier_schema,
            total_entries=merge_stats.total_entries,
            merged_entries=merge_stats.merged_entries,
            unmatched_entries_count=len(unmatched_report_rows),
            include_cutscenes=request.include_cutscenes,
            cutscene_output_files=(
                {relative: str(path.resolve()) for relative, path in cutscenes.output_files.items()}
                if cutscenes is not None else {}),
            cutscene_output_hashes=cutscenes.output_hashes if cutscenes is not None else {},
            cutscene_source_bundles=(
                cutscenes.source_bundles if cutscenes is not None else {}),
            cutscene_package_files=cutscene_package_files,
            cutscene_package_hashes=cutscene_package_hashes,
            cutscene_package_sizes=cutscene_package_sizes,
            cutscene_package_backend=cutscene_package_backend,
            cutscene_package_backend_version=cutscene_package_backend_version,
            cutscene_summary=cutscenes.summary if cutscenes is not None else CutsceneGenerationSummary(),
            cutscene_bundle_fingerprint=initial_bundle_fingerprint,
            cutscene_bundle_content_fingerprint=initial_bundle_content,
        )
        temporary_record = generation_dir / "generation.json.tmp"
        temporary_record.write_text(
            json.dumps(_record_payload(record), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_record, generation_dir / "generation.json")
        if cutscenes is not None:
            summary = cutscenes.summary
            report_progress(
                progress_callback,
                f"Cutscenes: {summary.sidecar_changed + summary.usm_changed} changed, "
                f"{summary.sidecar_skipped + summary.usm_skipped} skipped, "
                f"{summary.matched_cues}/{summary.primary_cues} primary cues matched, "
                f"{summary.output_bytes} output bytes",
                1, 1,
            )
        report_progress(progress_callback, "Publishing generation preview", 1, 1)
        published = True
        return record
    except GenerationError:
        raise
    except Exception as error:
        raise GenerationError(f"Generation failed: {error}") from error
    finally:
        # The UUID directory is created exclusively for this attempt. Keep it
        # only once a complete record has been returned, including callbacks.
        if not published:
            if generation_dir.resolve() != generations_root / generation_id:
                raise GenerationError("Failed generation directory moved; refusing unsafe cleanup")
            shutil.rmtree(generation_dir)


def _record_from_payload(payload: object) -> GenerationRecord:
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] not in (1, GENERATION_RECORD_SCHEMA)):
        raise ValueError("unsupported generation record schema")
    include_cutscenes = False
    cutscene_files, cutscene_hashes = {}, {}
    cutscene_source_bundles = {}
    cutscene_package_files, cutscene_package_hashes, cutscene_package_sizes = {}, {}, {}
    cutscene_package_backend = cutscene_package_backend_version = None
    cutscene_summary = CutsceneGenerationSummary()
    cutscene_fingerprint = None
    cutscene_content_fingerprint = None
    if payload["schema_version"] == 2:
        include_cutscenes = payload.get("include_cutscenes")
        if type(include_cutscenes) is not bool:
            raise ValueError("generation record cutscene option is malformed")
        cutscene_files, cutscene_hashes = (
            payload.get("cutscene_output_files"), payload.get("cutscene_output_hashes"))
        if (not isinstance(cutscene_files, dict) or not isinstance(cutscene_hashes, dict)
                or cutscene_files.keys() != cutscene_hashes.keys()
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for mapping in (cutscene_files, cutscene_hashes)
                       for key, value in mapping.items())
                or any(not _is_sha256(value) for value in cutscene_hashes.values())):
            raise ValueError("generation record cutscene outputs are malformed")
        for path in cutscene_files:
            if (safe_relative_path(path) != path
                    or PurePosixPath(path).suffix not in (".subs", ".usm")):
                raise ValueError("generation record cutscene virtual path is unsafe")
        raw_source_bundles = payload.get("cutscene_source_bundles", {})
        if (not isinstance(raw_source_bundles, dict)
                or any(not isinstance(path, str) or not isinstance(origins, list)
                       or not origins or any(not isinstance(origin, str) for origin in origins)
                       for path, origins in raw_source_bundles.items())):
            raise ValueError("generation record cutscene source provenance is malformed")
        for path, origins in raw_source_bundles.items():
            if (safe_relative_path(path) != path
                    or any(safe_relative_path(origin) != origin or not origin.endswith(".bundle")
                           for origin in origins)
                    or len({origin.casefold() for origin in origins}) != len(origins)):
                raise ValueError("generation record cutscene source bundle path is unsafe")
        cutscene_source_bundles = {
            path: tuple(origins) for path, origins in raw_source_bundles.items()
        }
        raw_package_files = payload.get("cutscene_package_files", {})
        raw_package_hashes = payload.get("cutscene_package_hashes", {})
        raw_package_sizes = payload.get("cutscene_package_sizes", {})
        if (not isinstance(raw_package_files, dict) or not isinstance(raw_package_hashes, dict)
                or not isinstance(raw_package_sizes, dict)
                or any(not isinstance(path, str) or not isinstance(value, str)
                       for path, value in raw_package_files.items())
                or raw_package_files.keys() != raw_package_hashes.keys()
                or raw_package_files.keys() != raw_package_sizes.keys()
                or any(not _is_sha256(value) for value in raw_package_hashes.values())
                or any(type(value) is not int or value < 0
                       for value in raw_package_sizes.values())):
            raise ValueError("generation record movie package inventory is malformed")
        allowed_package_targets = {
            "Mods/modW3DualSubtitleManager/content/metadata.store",
            "Mods/modW3DualSubtitleManager/content/bundles/movies.bundle",
        }
        if raw_package_files and set(raw_package_files) != allowed_package_targets:
            raise ValueError("generation record movie package target paths are malformed")
        cutscene_package_files = dict(raw_package_files)
        cutscene_package_hashes = dict(raw_package_hashes)
        cutscene_package_sizes = dict(raw_package_sizes)
        cutscene_package_backend = payload.get("cutscene_package_backend")
        cutscene_package_backend_version = payload.get("cutscene_package_backend_version")
        if (cutscene_package_backend is not None
                and (not isinstance(cutscene_package_backend, str) or not cutscene_package_backend)
                or cutscene_package_backend_version is not None
                and (not isinstance(cutscene_package_backend_version, str)
                     or not cutscene_package_backend_version)):
            raise ValueError("generation record movie package backend is malformed")
        if raw_package_files and (not cutscene_package_backend or not cutscene_package_backend_version):
            raise ValueError("generation record movie package backend is missing")
        summary = payload.get("cutscene_summary")
        if (not isinstance(summary, dict)
                or set(summary) != set(CutsceneGenerationSummary.__dataclass_fields__)
                or any(type(value) is not int or value < 0 for value in summary.values())
                or summary["matched_cues"] > min(summary["primary_cues"], summary["secondary_cues"])
                or summary["unmatched_count"] != (
                    summary["primary_cues"] + summary["secondary_cues"] - 2 * summary["matched_cues"])
                or any(summary[f"{kind}_discovered"] != sum(
                    summary[f"{kind}_{outcome}"] for outcome in ("changed", "skipped", "unchanged"))
                    for kind in ("sidecar", "usm"))):
            raise ValueError("generation record cutscene summary is malformed")
        cutscene_summary = CutsceneGenerationSummary(**summary)
        raw_fingerprint = payload.get("cutscene_bundle_fingerprint")
        if include_cutscenes:
            if (not isinstance(raw_fingerprint, dict)
                    or not _is_sha256(raw_fingerprint.get("digest"))
                    or not isinstance(raw_fingerprint.get("entries"), dict)):
                raise ValueError("generation record bundle fingerprint is malformed")
            for path, digest in raw_fingerprint["entries"].items():
                if (safe_relative_path(path) != path or not path.endswith(".bundle")
                        or not _is_sha256(digest)):
                    raise ValueError("generation record bundle identity is malformed")
            cutscene_fingerprint = ResourceFingerprint(
                dict(raw_fingerprint["entries"]), raw_fingerprint["digest"])
            raw_content = payload.get("cutscene_bundle_content_fingerprint")
            if raw_content is not None:
                if (not isinstance(raw_content, dict) or not _is_sha256(raw_content.get("digest"))
                        or not isinstance(raw_content.get("entries"), dict)
                        or raw_content["entries"].keys() != cutscene_fingerprint.entries.keys()
                        or any(not _is_sha256(digest) for digest in raw_content["entries"].values())):
                    raise ValueError("generation record bundle content fingerprint is malformed")
                cutscene_content_fingerprint = ResourceFingerprint(dict(raw_content["entries"]), raw_content["digest"])
        elif (cutscene_files or cutscene_hashes or cutscene_source_bundles
              or cutscene_package_files or cutscene_package_hashes or cutscene_package_sizes
              or cutscene_package_backend is not None or cutscene_package_backend_version is not None
              or raw_fingerprint is not None
              or payload.get("cutscene_bundle_content_fingerprint") is not None
              or cutscene_summary != CutsceneGenerationSummary()):
            raise ValueError("disabled cutscene generation contains cutscene data")
    if payload.get("codec_kind", "external") not in ("native", "external"):
        raise ValueError("unsupported generation codec kind")
    total_entries = payload.get("total_entries")
    merged_entries = payload.get("merged_entries")
    unmatched_entries_count = payload.get("unmatched_entries_count")
    if total_entries is None and merged_entries is None:
        # Older generation records predate merge statistics.
        pass
    elif (type(total_entries) is not int or type(merged_entries) is not int
          or total_entries < 0 or merged_entries < 0 or merged_entries > total_entries):
        raise ValueError("generation record merge statistics are malformed")
    if (unmatched_entries_count is not None
            and (type(unmatched_entries_count) is not int or unmatched_entries_count < 0)):
        raise ValueError("generation record unmatched count is malformed")
    classifier_schema = payload.get("classifier_schema_version")
    if classifier_schema is not None and (type(classifier_schema) is not int or classifier_schema < 1):
        raise ValueError("invalid classifier schema version")
    version = payload.get("game_version")
    fingerprint = payload.get("source_fingerprint")
    if not isinstance(version, dict) or not isinstance(fingerprint, dict):
        raise ValueError("generation record version or fingerprint is malformed")
    entries = fingerprint.get("entries")
    if (not isinstance(entries, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in entries.items())):
        raise ValueError("generation record source entries are malformed")
    if any(not _is_sha256(value) for value in entries.values()):
        raise ValueError("generation record contains an invalid source hash")
    output_files, output_hashes = payload.get("output_files"), payload.get("output_hashes")
    if (not isinstance(output_files, dict) or not isinstance(output_hashes, dict)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for mapping in (output_files, output_hashes)
                   for key, value in mapping.items())):
        raise ValueError("generation record outputs are malformed")
    if any(not _is_sha256(value) for value in output_hashes.values()):
        raise ValueError("generation record contains an invalid output hash")
    for name in ("generation_id", "game_root", "generation_dir", "converter_path", "app_version",
                 "primary_language", "secondary_language"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            raise ValueError(f"generation record {name} is malformed")
    for name in ("converter_sha256", "converter_version", "classifier_digest"):
        if payload.get(name) is not None and not isinstance(payload[name], str):
            raise ValueError(f"generation record {name} is malformed")
    if (not _is_sha256(payload.get("converter_sha256"))
            or not isinstance(fingerprint.get("digest"), str)
            or not _is_sha256(fingerprint.get("digest"))
            or (payload.get("classifier_digest") is not None
                and not _is_sha256(payload.get("classifier_digest")))
            or not all(isinstance(version.get(name), (str, type(None)))
                       for name in ("executable_version", "store_build_id", "executable_version_raw"))):
        raise ValueError("generation record version or digest is malformed")
    if (len(payload["generation_id"]) != 32
            or any(character not in "0123456789abcdef" for character in payload["generation_id"])
            or not Path(payload["generation_dir"]).is_absolute()
            or not Path(payload["game_root"]).is_absolute()
            or not Path(payload["converter_path"]).is_absolute()):
        raise ValueError("generation record contains an unsafe id or path")
    record = GenerationRecord(
        generation_id=payload["generation_id"],
        game_root=Path(payload["game_root"]),
        generation_dir=Path(payload["generation_dir"]),
        storefront=Storefront(payload["storefront"]),
        game_version=GameVersion(
            version["executable_version"], version.get("store_build_id"),
            version.get("executable_version_raw"),
        ),
        primary_language=payload["primary_language"],
        secondary_language=payload["secondary_language"],
        mode=MergeMode(payload["mode"]),
        source_fingerprint=ResourceFingerprint(dict(entries), fingerprint["digest"]),
        classifier_digest=payload.get("classifier_digest"),
        converter_path=payload["converter_path"],
        converter_sha256=payload.get("converter_sha256"),
        converter_version=payload.get("converter_version"),
        app_version=payload["app_version"],
        output_files=dict(output_files),
        output_hashes=dict(output_hashes),
        codec_kind=payload.get("codec_kind", "external"),
        classifier_schema_version=classifier_schema,
        total_entries=total_entries,
        merged_entries=merged_entries,
        unmatched_entries_count=unmatched_entries_count,
        include_cutscenes=include_cutscenes,
        cutscene_output_files=dict(cutscene_files),
        cutscene_output_hashes=dict(cutscene_hashes),
        cutscene_source_bundles=dict(cutscene_source_bundles),
        cutscene_package_files=dict(cutscene_package_files),
        cutscene_package_hashes=dict(cutscene_package_hashes),
        cutscene_package_sizes=dict(cutscene_package_sizes),
        cutscene_package_backend=cutscene_package_backend,
        cutscene_package_backend_version=cutscene_package_backend_version,
        cutscene_summary=cutscene_summary,
        cutscene_bundle_fingerprint=cutscene_fingerprint,
        cutscene_bundle_content_fingerprint=cutscene_content_fingerprint,
    )
    return record


def _is_sha256(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value.lower()))


def load_latest_generation_record(state_root: Path, game_root: Path) -> GenerationRecord | None:
    """Load the newest well-formed generation for this exact game root."""
    root = _normalized_game_root(game_root)
    supplied_state = Path(state_root).expanduser().resolve()
    generations = supplied_state / "games" / _game_root_hash(root) / "generations"
    try:
        generations.resolve(strict=True).relative_to(root)
    except ValueError:
        pass
    except OSError:
        return None
    else:
        raise GenerationError("Generation state must be outside the game folder")
    try:
        children = tuple(generations.iterdir())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise GenerationError(f"Cannot inspect generation history: {error}") from error
    candidates = []
    for directory in children:
        record_path = directory / "generation.json"
        try:
            if not directory.is_dir() or not record_path.is_file():
                continue
            if directory.resolve(strict=True) != directory:
                continue
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            record = _record_from_payload(payload)
            if (_normalized_game_root(record.game_root) != root
                    or _generation_directory(record, root) != directory.resolve(strict=True)
                    or not _verify_outputs(record, root, verify_cutscene_hashes=False)):
                continue
            candidates.append((record_path.stat().st_mtime_ns, record))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _generation_directory(record: GenerationRecord, game_root: Path) -> Path | None:
    raw_directory = Path(record.generation_dir)
    if not raw_directory.is_absolute():
        return None
    try:
        directory = raw_directory.resolve(strict=True)
        if raw_directory != directory:
            return None
    except OSError:
        return None

    root = _normalized_game_root(game_root)
    if directory.name != record.generation_id:
        return None
    generations = directory.parent
    game_state = generations.parent
    games = game_state.parent
    if (generations.name != "generations"
            or game_state.name != _game_root_hash(root)
            or games.name != "games"):
        return None
    try:
        directory.relative_to(root)
    except ValueError:
        pass
    else:
        return None
    return directory


def _safe_relative_output(relative: str) -> tuple[str, ...] | None:
    if not isinstance(relative, str) or not relative:
        return None
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in posix.parts or "." in posix.parts
            or posix.as_posix() != relative or "\\" in relative):
        return None
    try:
        safe_relative_path(relative)
    except ValueError:
        return None
    return posix.parts


def _verify_outputs(record: GenerationRecord, game_root: Path, *,
                    progress_callback: ProgressCallback | None = None,
                    verify_cutscene_hashes: bool = True) -> bool:
    """Verify staged resources and their movie bundle; startup skips huge hashes."""
    expected_outputs = {
        relative
        for relative in record.source_fingerprint.entries
        if (PurePosixPath(relative).suffix.casefold() == ".w3strings"
            and PurePosixPath(relative).stem.casefold() == record.primary_language.casefold())
    }
    if (not expected_outputs
            or set(record.output_files) != expected_outputs
            or record.output_files.keys() != record.output_hashes.keys()):
        return False
    generation_dir = _generation_directory(record, game_root)
    if generation_dir is None:
        return False
    if (type(record.include_cutscenes) is not bool
            or record.cutscene_output_files.keys() != record.cutscene_output_hashes.keys()
            or record.cutscene_package_files.keys() != record.cutscene_package_hashes.keys()
            or record.cutscene_package_files.keys() != record.cutscene_package_sizes.keys()):
        return False
    if record.include_cutscenes:
        if (record.cutscene_bundle_fingerprint is None
                or record.cutscene_bundle_content_fingerprint is None
                or record.cutscene_bundle_content_fingerprint.entries.keys() != record.cutscene_bundle_fingerprint.entries.keys()
                or record.cutscene_source_bundles.keys() != record.cutscene_output_files.keys()):
            return False
        if record.cutscene_output_files and set(record.cutscene_package_files) != {
                "Mods/modW3DualSubtitleManager/content/metadata.store",
                "Mods/modW3DualSubtitleManager/content/bundles/movies.bundle"}:
            return False
        if (record.cutscene_package_files
                and (record.cutscene_package_backend != MOVIE_PACKAGE_BACKEND
                     or record.cutscene_package_backend_version != MOVIE_PACKAGE_BACKEND_VERSION)):
            return False
        for relative, origins in record.cutscene_source_bundles.items():
            if (safe_relative_path(relative) != relative or not origins
                    or any(safe_relative_path(origin) != origin or not origin.endswith(".bundle")
                           for origin in origins)):
                return False
    elif (record.cutscene_output_files or record.cutscene_output_hashes
          or record.cutscene_source_bundles or record.cutscene_package_files
          or record.cutscene_package_hashes or record.cutscene_package_sizes
          or record.cutscene_package_backend is not None
          or record.cutscene_package_backend_version is not None
          or record.cutscene_bundle_fingerprint is not None
          or record.cutscene_bundle_content_fingerprint is not None
          or record.cutscene_summary != CutsceneGenerationSummary()):
        return False
    total = (len(record.output_files) + len(record.cutscene_output_files)
             + len(record.cutscene_package_files))
    report_progress(progress_callback, "Checking generated output files", 0, total)
    cutscene_bytes = 0
    outputs = [(relative, raw_path, record.output_hashes.get(relative), "interactive")
               for relative, raw_path in record.output_files.items()]
    outputs.extend((relative, raw_path, record.cutscene_output_hashes.get(relative), "cutscene")
                   for relative, raw_path in record.cutscene_output_files.items())
    outputs.extend((relative, raw_path, record.cutscene_package_hashes.get(relative), "package")
                   for relative, raw_path in record.cutscene_package_files.items())
    actual_packages = {}
    for number, (relative, raw_path, digest, kind) in enumerate(outputs, 1):
        parts = _safe_relative_output(relative)
        if parts is None or not _is_sha256(digest):
            return False
        if kind == "cutscene" and (safe_relative_path(relative) != relative
                                    or PurePosixPath(relative).suffix not in (".subs", ".usm")):
            return False
        if kind == "package" and relative not in {
                "Mods/modW3DualSubtitleManager/content/metadata.store",
                "Mods/modW3DualSubtitleManager/content/bundles/movies.bundle"}:
            return False
        parent = (generation_dir / "cutscenes" if kind == "cutscene" else
                  generation_dir / "movie_packages" if kind == "package" else generation_dir)
        expected = parent.joinpath(*parts)
        try:
            actual_path = Path(raw_path)
            if not actual_path.is_absolute():
                return False
            actual = actual_path.resolve(strict=True)
            expected = expected.resolve(strict=True)
            if actual != expected or not actual.is_file():
                return False
            try:
                actual.relative_to(generation_dir)
            except ValueError:
                return False
            if kind == "cutscene":
                cutscene_bytes += actual.stat().st_size
            if kind == "package":
                if actual.stat().st_size != record.cutscene_package_sizes[relative]:
                    return False
                actual_packages[relative] = actual
            must_hash = (kind == "interactive"
                         or (kind == "cutscene"
                             and (verify_cutscene_hashes or actual.suffix.casefold() != ".usm"))
                         or (kind == "package"
                             and (verify_cutscene_hashes or actual.name.casefold() != "movies.bundle")))
            if must_hash and _hash_file(expected) != digest:
                return False
        except (OSError, ValueError, TypeError, RuntimeError):
            return False
        report_progress(progress_callback, "Checking generated output files", number, total)
    if cutscene_bytes != record.cutscene_summary.output_bytes:
        return False
    if record.cutscene_output_files:
        bundle_target = "Mods/modW3DualSubtitleManager/content/bundles/movies.bundle"
        metadata_target = "Mods/modW3DualSubtitleManager/content/metadata.store"
        bundle = actual_packages.get(bundle_target)
        metadata = actual_packages.get(metadata_target)
        if bundle is None or metadata is None:
            return False
        for relative in record.cutscene_output_files:
            if (PurePosixPath(relative).suffix == ".subs"
                    and movie_companion_path(relative) not in record.cutscene_output_files):
                return False
        if verify_cutscene_hashes:
            try:
                report_progress(progress_callback, "Validating packaged movie resources", 0, 1)
                packaged_resources = validate_movie_package(
                    bundle, metadata, progress_callback=progress_callback,
                )
                if (set(packaged_resources) != set(record.cutscene_output_files)
                        or any(record.cutscene_output_hashes.get(path) != digest
                               for path, digest in packaged_resources.items())):
                    return False
                report_progress(progress_callback, "Validating packaged movie resources", 1, 1)
            except (OSError, ValueError, RuntimeError):
                return False
    return True


def compare_generation(record: GenerationRecord, game: GameInstallation, *,
                       progress_callback: ProgressCallback | None = None) -> Freshness:
    """Compare staged inputs and outputs with a freshly scanned install."""
    if _normalized_game_root(record.game_root) != _normalized_game_root(game.root):
        return Freshness.STALE
    if record.app_version != APP_VERSION:
        return Freshness.STALE
    if record.include_cutscenes and game.version != record.game_version:
        return Freshness.STALE
    try:
        sources = _source_paths(game, record.primary_language, record.secondary_language)
        current_fingerprint = _fingerprint_sources(sources, progress_callback)
        if record.include_cutscenes:
            report_progress(progress_callback, "Checking cutscene bundle metadata", 0, 1)
            current_bundles = fingerprint_cutscene_bundles(game)
            if current_bundles != record.cutscene_bundle_fingerprint:
                return Freshness.STALE
            report_progress(progress_callback, "Checking cutscene bundle metadata", 1, 1)
    except (OSError, PermissionError):
        return Freshness.UNREADABLE
    except (GenerationError, ValueError):
        return Freshness.STALE
    if current_fingerprint != record.source_fingerprint:
        return Freshness.STALE
    if not _verify_outputs(record, game.root, progress_callback=progress_callback,
                           verify_cutscene_hashes=False):
        return Freshness.STALE
    if record.codec_kind == "native":
        if record.converter_version != NativeW3StringsCodec.version:
            return Freshness.STALE
        try:
            report_progress(progress_callback, "Checking codec implementation", 0, 1)
            _, current_codec_digest = _hash_executable(NativeW3StringsCodec())
        except GenerationError:
            return Freshness.UNREADABLE
        if record.converter_sha256 != current_codec_digest:
            return Freshness.STALE
        report_progress(progress_callback, "Checking codec implementation", 1, 1)
    if game.storefront is not record.storefront:
        return Freshness.STALE

    if game.version != record.game_version:
        return Freshness.VERSION_METADATA_CHANGED_ONLY
    return Freshness.CURRENT
