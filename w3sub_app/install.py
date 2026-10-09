"""Transactional install lifecycle for generated Witcher 3 string resources."""
import csv
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import shutil
import stat
import uuid

from . import generation, storefronts
from .cutscene_generation import fingerprint_cutscene_bundles, fingerprint_cutscene_bundle_contents
from .game import scan_game
from .progress import ProgressCallback, report_progress
from .models import (
    Freshness,
    GameInstallation,
    GameCandidate,
    GameVersion,
    GenerationRecord,
    GenerationProvenance,
    InstallComparison,
    InstallManifest,
    InstallTarget,
    MergeMode,
    ResourceFingerprint,
    Storefront,
    UninstallResult,
)


INSTALL_MANIFEST_SCHEMA = 3
MOD_ROOT = "Mods/modW3DualSubtitleManager"
MOD_CONTENT = MOD_ROOT + "/content"


class InstallError(RuntimeError):
    """An install operation failed without hiding rollback diagnostics."""

    def __init__(self, message, *, target_paths=(), rollback_errors=()):
        super().__init__(message)
        self.target_paths = tuple(target_paths)
        self.rollback_errors = tuple(rollback_errors)


def _running_game_processes() -> tuple[str, ...]:
    """Return running Witcher executable image names on Windows."""
    if os.name != "nt":
        return ()
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(f"Cannot confirm the game is closed: {error}") from error
    if result.returncode != 0:
        raise InstallError("Cannot confirm the game is closed; tasklist failed")
    try:
        images = tuple(
            row[0].casefold()
            for row in csv.reader(result.stdout.splitlines())
            if row and row[0].casefold() == "witcher3.exe"
        )
    except csv.Error as error:
        raise InstallError(f"Cannot parse the running-process list: {error}") from error
    return images


def _ensure_game_closed() -> None:
    running = _running_game_processes()
    if running:
        raise InstallError("The Witcher 3 process is running; close it before changing game files")


def _normalized_path(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(_normalized_path(path))))


def _root_hash(root: Path) -> str:
    return hashlib.sha256(_path_identity(root).encode("utf-8")).hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        _normalized_path(path).relative_to(_normalized_path(parent))
        return True
    except ValueError:
        return False


def _state_directory(state_root: Path, game_root: Path) -> Path:
    root = _normalized_path(game_root)
    supplied = _normalized_path(Path(state_root))
    key = _root_hash(root)
    if supplied.name == key and supplied.parent.name == "games":
        directory = supplied
    else:
        directory = supplied / "games" / key
    directory = directory.resolve()
    if _inside(directory, root):
        raise InstallError("Install state must stay outside the game folder")
    return directory


def _validate_state_directory(directory: Path, game_root: Path) -> Path:
    root = _normalized_path(game_root)
    state = _normalized_path(directory)
    if (state.name != _root_hash(root) or state.parent.name != "games"
            or _inside(state, root)):
        raise InstallError("Install manifest is not stored in this game's app-data folder")
    return state


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_mod_target(relative: str) -> bool:
    return relative.startswith(MOD_CONTENT + "/")


def mod_resource_target(relative: str) -> str:
    """Map a game-relative string resource into the manager mod's content root."""
    parts = generation._safe_relative_output(relative)
    if parts is None or PurePosixPath(relative).suffix.casefold() != ".w3strings":
        raise InstallError(f"Unsafe manager-mod string resource path: {relative!r}")
    # The manager mod's content directory already represents the game's
    # top-level content directory, so do not copy that component a second time.
    if parts[0].casefold() == "content":
        parts = parts[1:]
    if not parts:
        raise InstallError(f"Manager-mod string resource path is empty: {relative!r}")
    target = MOD_CONTENT + "/" + "/".join(parts)
    _safe_relative(target)
    return target


def _safe_relative(relative: str) -> tuple[str, ...]:
    parts = generation._safe_relative_output(relative)
    if parts is not None:
        suffix = PurePosixPath(relative).suffix.casefold()
        package_targets = {
            (MOD_CONTENT + "/metadata.store").casefold(),
            (MOD_CONTENT + "/bundles/movies.bundle").casefold(),
        }
        if (_is_mod_target(relative) and len(parts) > 3
                and (suffix in {".subs", ".usm", ".w3strings"}
                     or relative.casefold() in package_targets)):
            return parts
    raise InstallError(f"Unsafe game target path: {relative!r}")


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _target_path(game_root: Path, relative: str, *, allow_missing: bool = False) -> Path:
    parts = _safe_relative(relative)
    root = _normalized_path(game_root)
    if not root.is_dir() or _is_reparse(Path(game_root)) or _is_reparse(root):
        raise InstallError(f"Game folder is unavailable or redirected: {root}")
    candidate = root.joinpath(*parts)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            if _is_reparse(current):
                raise InstallError(f"Game target contains a symbolic link or junction: {relative}")
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            if allow_missing and _is_mod_target(relative):
                return candidate
            raise InstallError(f"Game target is missing: {relative}")
        except OSError as error:
            raise InstallError(f"Cannot access game target {relative}: {error}") from error
        if not _inside(resolved, root):
            raise InstallError(f"Game target resolves outside the selected folder: {relative}")
        if index < len(parts) - 1 and not resolved.is_dir():
            raise InstallError(f"Game target parent is not a directory: {relative}")
    if not candidate.is_file():
        raise InstallError(f"Game target is not a regular file: {relative}")
    return candidate


def _current_digest(root: Path, relative: str) -> str | None:
    path = _target_path(root, relative, allow_missing=True)
    try:
        return _hash_file(path)
    except FileNotFoundError:
        if _is_mod_target(relative):
            return None
        raise


def _directory_path(root: Path, relative: str) -> Path:
    parts = generation._safe_relative_output(relative)
    if (parts is None or (relative != "Mods" and relative != MOD_ROOT
            and not relative.startswith(MOD_ROOT + "/"))):
        raise InstallError(f"Unsafe managed directory: {relative!r}")
    current = _normalized_path(root)
    if _is_reparse(current) or not current.is_dir():
        raise InstallError("Game root is redirected or unavailable")
    for part in parts:
        current = current / part
        try:
            if _is_reparse(current) or not current.is_dir():
                raise InstallError(f"Managed directory is redirected or not a directory: {relative}")
            if not _inside(current.resolve(strict=True), root):
                raise InstallError(f"Managed directory escapes the game root: {relative}")
        except FileNotFoundError:
            break
    return _normalized_path(root).joinpath(*parts)


def _validate_mod_inventory(root: Path, manifest: InstallManifest | None, *,
                            staged_paths: tuple[Path, ...] = ()) -> None:
    mod = _directory_path(root, MOD_ROOT)
    if not mod.exists():
        return
    owned = manifest is not None and (MOD_ROOT in manifest.created_directories
        or any(_is_mod_target(relative) for relative in manifest.target_files))
    if not owned:
        raise InstallError(f"Unmanaged mod directory already exists: {MOD_ROOT}",
                           target_paths=(MOD_ROOT,))
    tracked = {relative.casefold() for relative in manifest.target_files}
    tracked.update(path.relative_to(root).as_posix().casefold() for path in staged_paths)
    pending = [mod]
    while pending:
        directory = pending.pop()
        _directory_path(root, directory.relative_to(root).as_posix())
        for child in directory.iterdir():
            relative = child.relative_to(root).as_posix()
            if _is_reparse(child):
                raise InstallError(f"Managed mod contains a symbolic link or junction: {relative}",
                                   target_paths=(relative,))
            if child.is_dir():
                pending.append(child)
            elif not child.is_file() or relative.casefold() not in tracked:
                raise InstallError(f"Unmanaged file in manager mod: {relative}",
                                   target_paths=(relative,))


def _create_target_parents(root: Path, relative: str, created: set[str], *,
                           owned_root: bool = True) -> None:
    if not _is_mod_target(relative):
        _target_path(root, relative)
        return
    parts = _safe_relative(relative)
    for length in range(1, len(parts)):
        directory = "/".join(parts[:length])
        path = _directory_path(root, directory)
        try:
            path.mkdir()
        except FileExistsError:
            if directory == MOD_ROOT and not owned_root and directory not in created:
                raise InstallError(f"Unmanaged mod directory appeared during staging: {MOD_ROOT}",
                                   target_paths=(MOD_ROOT,))
        else:
            created.add(directory)
        _directory_path(root, directory)


def _remove_empty_created_directories(root: Path, directories) -> set[str]:
    removed = set()
    for relative in sorted(set(directories), key=lambda value: (value.count("/"), value), reverse=True):
        try:
            path = _directory_path(root, relative)
            path.rmdir()
            removed.add(relative)
        except FileNotFoundError:
            pass
        except (OSError, InstallError):
            pass  # Nonempty directories retain user files and directories.
    return removed


def _version_payload(version: GameVersion) -> dict[str, str | None]:
    return {
        "executable_version": version.executable_version,
        "store_build_id": version.store_build_id,
        "executable_version_raw": version.executable_version_raw,
    }


def _fingerprint_payload(fingerprint: ResourceFingerprint) -> dict[str, object]:
    return {"entries": dict(sorted(fingerprint.entries.items())), "digest": fingerprint.digest}


def _manifest_payload(manifest: InstallManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "install_id": manifest.install_id,
        "game_root": str(manifest.game_root),
        "state_directory": str(manifest.state_directory),
        "storefront": manifest.storefront.value,
        "store_build_id": manifest.store_build_id,
        "generation_id": manifest.generation_id,
        "generation_version": _version_payload(manifest.generation_version),
        "install_version": _version_payload(manifest.install_version),
        "source_fingerprint": _fingerprint_payload(manifest.source_fingerprint),
        "primary_language": manifest.primary_language,
        "secondary_language": manifest.secondary_language,
        "mode": manifest.mode.value,
        "classifier_digest": manifest.classifier_digest,
        "generation_provenance": (asdict(manifest.generation_provenance)
                                  if manifest.generation_provenance is not None else None),
        "target_files": {
            relative: {
                "relative_path": target.relative_path,
                "installed_sha256": target.installed_sha256,
            }
            for relative, target in sorted(manifest.target_files.items())
        },
        "active": manifest.active,
        "conflicted": manifest.conflicted,
        "conflict_paths": list(manifest.conflict_paths),
        "prepared": manifest.prepared,
        "created_directories": list(manifest.created_directories),
        "cutscene_bundle_content_fingerprint": (
            _fingerprint_payload(manifest.cutscene_bundle_content_fingerprint)
            if manifest.cutscene_bundle_content_fingerprint is not None else None),
        "cutscene_bundle_fingerprint": (
            _fingerprint_payload(manifest.cutscene_bundle_fingerprint)
            if manifest.cutscene_bundle_fingerprint is not None else None),
    }


def _version_from_payload(payload: dict) -> GameVersion:
    return GameVersion(
        executable_version=payload["executable_version"],
        store_build_id=payload.get("store_build_id"),
        executable_version_raw=payload.get("executable_version_raw"),
    )


def _manifest_from_payload(payload: dict) -> InstallManifest:
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] not in (1, 2, INSTALL_MANIFEST_SCHEMA):
        raise ValueError("unsupported install manifest schema")
    for name in ("active", "conflicted", "prepared"):
        if name in payload and not isinstance(payload[name], bool):
            raise ValueError(f"{name} must be a boolean")
    if not isinstance(payload.get("target_files"), dict):
        raise ValueError("target_files must be an object")
    if not isinstance(payload.get("conflict_paths", []), list) or any(
            not isinstance(path, str) for path in payload.get("conflict_paths", [])):
        raise ValueError("conflict_paths must be a list of paths")
    if payload["schema_version"] >= 2 and (not isinstance(payload.get("created_directories", []), list)
            or any(not isinstance(path, str) for path in payload.get("created_directories", []))):
        raise ValueError("created_directories must be a list of paths")
    raw_fingerprint = payload["source_fingerprint"]
    if not isinstance(raw_fingerprint, dict) or not isinstance(raw_fingerprint.get("entries"), dict):
        raise ValueError("source_fingerprint must contain an entries object")
    fingerprint = ResourceFingerprint(dict(raw_fingerprint["entries"]), raw_fingerprint["digest"])
    raw_provenance = payload.get("generation_provenance")
    if raw_provenance is not None and not isinstance(raw_provenance, dict):
        raise ValueError("generation_provenance must be an object or null")
    provenance = GenerationProvenance(**raw_provenance) if raw_provenance is not None else None
    raw_cutscene = payload.get("cutscene_bundle_fingerprint") if payload["schema_version"] >= 2 else None
    cutscene_fingerprint = None
    if raw_cutscene is not None:
        if not isinstance(raw_cutscene, dict) or not isinstance(raw_cutscene.get("entries"), dict):
            raise ValueError("cutscene_bundle_fingerprint must contain an entries object")
        cutscene_fingerprint = ResourceFingerprint(dict(raw_cutscene["entries"]), raw_cutscene["digest"])
    raw_content = payload.get("cutscene_bundle_content_fingerprint") if payload["schema_version"] >= 2 else None
    content_fingerprint = None
    if raw_content is not None:
        if not isinstance(raw_content, dict) or not isinstance(raw_content.get("entries"), dict):
            raise ValueError("cutscene_bundle_content_fingerprint must contain an entries object")
        content_fingerprint = ResourceFingerprint(dict(raw_content["entries"]), raw_content["digest"])
    targets = {}
    for relative, entry in payload["target_files"].items():
        if not isinstance(entry, dict) or entry.get("relative_path") != relative:
            raise ValueError(f"target path key mismatch: {relative}")
        if not _is_mod_target(relative):
            raise InstallError(
                "This install contains direct game-file targets. This version only manages "
                "the manager mod; restore the game's files through your storefront before continuing."
            )
        targets[relative] = InstallTarget(
            relative_path=relative,
            installed_sha256=entry["installed_sha256"],
        )
    return InstallManifest(
        schema_version=INSTALL_MANIFEST_SCHEMA,
        install_id=payload["install_id"],
        game_root=Path(payload["game_root"]),
        state_directory=Path(payload["state_directory"]),
        storefront=Storefront(payload["storefront"]),
        store_build_id=payload.get("store_build_id"),
        generation_id=payload["generation_id"],
        generation_version=_version_from_payload(payload["generation_version"]),
        install_version=_version_from_payload(payload["install_version"]),
        source_fingerprint=fingerprint,
        primary_language=payload["primary_language"],
        secondary_language=payload["secondary_language"],
        mode=MergeMode(payload["mode"]),
        classifier_digest=payload.get("classifier_digest"),
        target_files=targets,
        active=bool(payload["active"]),
        conflicted=bool(payload.get("conflicted", False)),
        conflict_paths=tuple(payload.get("conflict_paths", ())),
        prepared=bool(payload.get("prepared", False)),
        generation_provenance=provenance,
        cutscene_bundle_fingerprint=cutscene_fingerprint,
        cutscene_bundle_content_fingerprint=content_fingerprint,
        created_directories=tuple(payload.get("created_directories", ())) if payload["schema_version"] >= 2 else (),
    )


def _atomic_write_manifest(manifest: InstallManifest) -> None:
    state = _validate_state_directory(manifest.state_directory, manifest.game_root)
    state.mkdir(parents=True, exist_ok=True)
    if _is_reparse(Path(manifest.state_directory)) or _is_reparse(state.parent) or _is_reparse(state):
        raise InstallError("Cannot save a manifest through a redirected game-state directory")
    final = state / "install.json"
    temporary = state / f".install-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(_manifest_payload(manifest), output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise InstallError(f"Cannot save install manifest: {error}",
                           target_paths=manifest.target_files) from error


def _migrate_legacy_content_resource_targets(manifest: InstallManifest) -> InstallManifest:
    """Adopt exact manager-mod string files at their corrected content-root paths.

    Earlier builds duplicated the game's leading ``content/`` directory under
    the mod's own ``content/`` directory. Migrate only when the old target is
    absent and the corrected target hashes to the exact bytes in the manifest.
    This updates app state only; it never moves, overwrites, or removes a game
    file. Any uncertain or edited target remains a conflict for review.
    """
    if not manifest.active or manifest.prepared:
        return manifest
    old_prefix = MOD_CONTENT + "/content/"
    old_prefix_folded = old_prefix.casefold()
    occupied = {relative.casefold() for relative in manifest.target_files}
    remapped: dict[str, str] = {}
    root = _normalized_path(manifest.game_root)
    for relative, target in manifest.target_files.items():
        if (not relative.casefold().startswith(old_prefix_folded)
                or PurePosixPath(relative).suffix.casefold() != ".w3strings"):
            continue
        corrected = MOD_CONTENT + "/" + relative[len(old_prefix):]
        if corrected.casefold() in occupied:
            continue
        try:
            if (_current_digest(root, relative) is None
                    and _current_digest(root, corrected) == target.installed_sha256):
                remapped[relative] = corrected
        except (InstallError, OSError):
            continue
    if not remapped or len({path.casefold() for path in remapped.values()}) != len(remapped):
        return manifest

    target_files = {
        remapped.get(relative, relative): InstallTarget(
            remapped.get(relative, relative), target.installed_sha256,
        )
        for relative, target in manifest.target_files.items()
    }
    if len({path.casefold() for path in target_files}) != len(target_files):
        return manifest

    # Keep cleanup ownership aligned to directories that now exist along the
    # adopted targets. Empty directories are removed only after owned files are
    # safely uninstalled; nonempty directories are always preserved.
    created = set()
    try:
        for directory in manifest.created_directories:
            if _directory_path(root, directory).is_dir():
                created.add(directory)
        for relative in remapped.values():
            parts = _safe_relative(relative)
            for length in range(1, len(parts)):
                directory = "/".join(parts[:length])
                if directory == "Mods" or directory == MOD_ROOT or directory.startswith(MOD_ROOT + "/"):
                    if _directory_path(root, directory).is_dir():
                        created.add(directory)
    except (InstallError, OSError):
        return manifest

    migrated = replace(
        manifest,
        target_files=target_files,
        created_directories=tuple(sorted(created)),
        conflicted=False,
        conflict_paths=(),
    )
    try:
        _validate_manifest(migrated, root)
        _atomic_write_manifest(migrated)
    except InstallError:
        return manifest
    return migrated


def _validate_manifest(manifest: InstallManifest, game_root: Path) -> None:
    if not isinstance(manifest, InstallManifest):
        raise InstallError("Install manifest has an unsupported type")
    _validate_provenance(manifest.generation_provenance)
    root = _normalized_path(game_root)
    if _path_identity(manifest.game_root) != _path_identity(root):
        raise InstallError("Install manifest belongs to a different game folder")
    _validate_state_directory(manifest.state_directory, root)
    if (not isinstance(manifest.install_id, str) or len(manifest.install_id) != 32
            or any(character not in "0123456789abcdef" for character in manifest.install_id)):
        raise InstallError("Install manifest has an invalid install id")
    if any(not isinstance(value, bool) for value in
           (manifest.active, manifest.conflicted, manifest.prepared)):
        raise InstallError("Install manifest contains invalid lifecycle flags")
    if not isinstance(manifest.target_files, dict) or any(
            not isinstance(path, str) for path in manifest.target_files):
        raise InstallError("Install manifest target inventory is invalid")
    if any(not isinstance(target, InstallTarget)
           for target in manifest.target_files.values()):
        raise InstallError("Install manifest target record is invalid")
    if set(manifest.target_files) != {target.relative_path for target in manifest.target_files.values()}:
        raise InstallError("Install manifest contains inconsistent target paths")
    if manifest.schema_version not in (1, 2, INSTALL_MANIFEST_SCHEMA):
        raise InstallError("Install manifest has an unsupported schema")
    if (not isinstance(manifest.created_directories, tuple)
            or any(not isinstance(path, str) for path in manifest.created_directories)):
        raise InstallError("Install manifest directory inventory is invalid")
    for directory in manifest.created_directories:
        _directory_path(root, directory)
    fingerprint = manifest.cutscene_bundle_fingerprint
    if fingerprint is not None:
        if (not isinstance(fingerprint, ResourceFingerprint) or not _is_digest(fingerprint.digest)
                or not isinstance(fingerprint.entries, dict)
                or any(generation._safe_relative_output(path) is None or not _is_digest(digest)
                       for path, digest in fingerprint.entries.items())):
            raise InstallError("Install cutscene bundle fingerprint is invalid")
    content = manifest.cutscene_bundle_content_fingerprint
    if content is not None:
        if (fingerprint is None or not isinstance(content, ResourceFingerprint)
                or not isinstance(content.entries, dict) or not _is_digest(content.digest)
                or content.entries.keys() != fingerprint.entries.keys()
                or any(not _is_digest(digest) for digest in content.entries.values())):
            raise InstallError("Install cutscene bundle content fingerprint is invalid")
    folded = [relative.casefold() for relative in manifest.target_files]
    if len(folded) != len(set(folded)):
        raise InstallError("Install manifest contains aliased target paths")
    for relative, target in manifest.target_files.items():
        _safe_relative(relative)
        if not _is_digest(target.installed_sha256):
            raise InstallError(f"Install manifest has invalid target metadata for {relative}")
        if not _is_mod_target(relative):
            raise InstallError(f"Install manifest target is outside the manager mod: {relative}")


def _is_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value.lower()))


def _validate_provenance(provenance: GenerationProvenance | None) -> None:
    if provenance is None:  # Older manifests did not carry this snapshot.
        return
    if (not isinstance(provenance, GenerationProvenance)
            or provenance.codec_kind not in ("native", "external")
            or not isinstance(provenance.converter_path, str)
            or not Path(provenance.converter_path).is_absolute()
            or not _is_digest(provenance.converter_sha256)
            or not isinstance(provenance.converter_version, (str, type(None)))
            or not isinstance(provenance.app_version, str) or not provenance.app_version
            or (provenance.classifier_schema_version is not None and
                (type(provenance.classifier_schema_version) is not int or
                 provenance.classifier_schema_version < 1))):
        raise InstallError("Install generation provenance is malformed")


def _load_manifest_file(path: Path, game_root: Path | None = None) -> InstallManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = _manifest_from_payload(payload)
        _validate_manifest(manifest, game_root or manifest.game_root)
        expected_path = manifest.state_directory / "install.json"
        if _normalized_path(path) != _normalized_path(expected_path):
            raise ValueError("manifest is stored outside its declared state directory")
        return _migrate_legacy_content_resource_targets(manifest)
    except (OSError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, InstallError):
            raise
        raise InstallError(f"Cannot load install manifest {path}: {error}") from error


def load_install_manifest(state_root: Path, game_root: Path | None = None) -> InstallManifest | None:
    """Load a manifest from a per-game state folder or an unambiguous app root."""
    state_root = _normalized_path(Path(state_root))
    direct = state_root / "install.json"
    if direct.is_file():
        return _load_manifest_file(direct, game_root)

    games = state_root / "games"
    if not games.is_dir():
        return None
    candidates = []
    if game_root is not None:
        expected = games / _root_hash(game_root) / "install.json"
        if expected.is_file():
            return _load_manifest_file(expected, game_root)
        return None
    for child in games.iterdir():
        path = child / "install.json"
        if path.is_file():
            candidates.append(path)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise InstallError("Multiple game manifests exist; pass the selected game root")
    return _load_manifest_file(candidates[0])


class _OperationLock:
    def __init__(self, state_directory: Path):
        self.path = state_directory / "operation.lock"
        self.token = f"{os.getpid()}:{uuid.uuid4().hex}\n".encode("ascii")
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600
            )
        except FileExistsError as error:
            raise InstallError(
                f"An install operation lock already exists at {self.path}; "
                "it may be stale and needs explicit recovery"
            ) from error
        try:
            os.write(descriptor, self.token)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.acquired = True
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        if self.acquired:
            try:
                if self.path.read_bytes() == self.token:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Leave an unreadable lock in place; the next operation will
                # require explicit recovery rather than risk overlapping work.
                pass


def _copy_new(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    created_destination = False
    try:
        with Path(source).open("rb") as input_file:
            with destination.open("xb") as output_file:
                created_destination = True
                for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                    digest.update(chunk)
                    output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
    except OSError:
        if created_destination:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return digest.hexdigest()


def _generation_freshness(record: GenerationRecord, game: GameInstallation,
                          progress_callback: ProgressCallback | None = None) -> Freshness:
    if _path_identity(record.game_root) != _path_identity(game.root):
        return Freshness.STALE
    return generation.compare_generation(record, game, progress_callback=progress_callback)


def _rescan_game(game: GameInstallation) -> GameInstallation:
    candidate = storefronts.refresh_candidate(GameCandidate(
        game.root, game.storefront, "install operation", game.version.store_build_id))
    return scan_game(candidate.root, candidate.storefront, candidate.store_build_id)


def _revalidate_before_mutation(record: GenerationRecord, game: GameInstallation,
                                progress_callback: ProgressCallback | None = None) -> None:
    """Called under the operation lock after staging, before any game replacement."""
    report_progress(progress_callback, "Rechecking game before replacement", 0, None)
    _ensure_game_closed()
    try:
        current = _rescan_game(game)
        if current.version != game.version or current.storefront is not game.storefront:
            raise InstallError("Game version/storefront changed during operation; rescan and retry")
        freshness = _generation_freshness(
            record, current, progress_callback=progress_callback,
        )
        if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(f"Generation became {freshness.value} before mutation; regenerate")
        if record.include_cutscenes:
            if (record.cutscene_bundle_content_fingerprint is None or
                    fingerprint_cutscene_bundle_contents(
                        current, progress_callback=progress_callback, phase="Checking cutscene bundle content identity")
                    != record.cutscene_bundle_content_fingerprint):
                raise InstallError("Source bundle contents changed before mutation; regenerate")
        if not generation._verify_outputs(
                record, game.root, progress_callback=progress_callback,
                verify_cutscene_hashes=False):
            raise InstallError("Generated outputs changed immediately before mutation")
    except InstallError:
        raise
    except Exception as error:
        raise InstallError(f"Cannot revalidate game immediately before mutation: {error}") from error


def _verified_outputs(record: GenerationRecord, game: GameInstallation, *,
                      progress_callback: ProgressCallback | None = None) -> dict[str, Path]:
    if not generation._verify_outputs(record, game.root, progress_callback=progress_callback):
        raise InstallError("Generation output inventory or hash is invalid")
    outputs = {mod_resource_target(relative): Path(raw)
               for relative, raw in record.output_files.items()}
    outputs.update({relative: Path(raw)
                    for relative, raw in record.cutscene_package_files.items()})
    folded = [relative.casefold() for relative in outputs]
    if len(folded) != len(set(folded)):
        raise InstallError("Generation contains aliased install targets")
    for relative, source in outputs.items():
        _safe_relative(relative)
        _target_path(game.root, relative, allow_missing=_is_mod_target(relative))
        if _is_reparse(source) or not source.is_file():
            raise InstallError(f"Generated output is unavailable: {relative}")
    return outputs


def _output_hashes(record: GenerationRecord) -> dict[str, str]:
    return {
        **{mod_resource_target(relative): digest
           for relative, digest in record.output_hashes.items()},
        **{relative: digest for relative, digest in record.cutscene_package_hashes.items()},
    }


def _make_manifest(game: GameInstallation, record: GenerationRecord,
                   state_directory: Path, install_id: str,
                   targets: dict[str, InstallTarget], *,
                   active: bool, conflicted: bool = False,
                   conflict_paths: tuple[str, ...] = (),
                   prepared: bool = False) -> InstallManifest:
    provenance = GenerationProvenance(record.codec_kind, record.converter_path,
                    record.converter_sha256, record.converter_version, record.app_version,
                    record.classifier_schema_version)
    _validate_provenance(provenance)
    return InstallManifest(
        schema_version=INSTALL_MANIFEST_SCHEMA,
        install_id=install_id,
        game_root=_normalized_path(game.root),
        state_directory=state_directory,
        storefront=game.storefront,
        store_build_id=game.version.store_build_id,
        generation_id=record.generation_id,
        generation_version=record.game_version,
        install_version=game.version,
        source_fingerprint=record.source_fingerprint,
        primary_language=record.primary_language,
        secondary_language=record.secondary_language,
        mode=record.mode,
        classifier_digest=record.classifier_digest,
        target_files=dict(targets),
        active=active,
        conflicted=conflicted,
        conflict_paths=tuple(conflict_paths),
        prepared=prepared,
        generation_provenance=provenance,
        cutscene_bundle_fingerprint=record.cutscene_bundle_fingerprint,
        cutscene_bundle_content_fingerprint=record.cutscene_bundle_content_fingerprint,
    )


def _publish_new(stage: Path, target: Path) -> None:
    """Publish a sibling stage atomically without replacing an existing name."""
    if os.name == "nt":
        # Windows rename refuses an existing destination on every supported
        # game filesystem, without requiring hard-link support.
        os.rename(stage, target)
    else:
        os.link(stage, target)
        stage.unlink()


class _ModQuarantine:
    """Pin one Windows file against writes/deletes, then rename by its handle.

    Path-based unlink/replace cannot discard a name that changed after a hash
    check. This handle denies write/delete sharing, and every rename/disposition
    operates on that exact opened object. Unsupported filesystems fail closed.
    """

    def __init__(self, root: Path, relative: str):
        if os.name != "nt":
            raise InstallError("Safe managed-mod mutations require Windows file sharing")
        import ctypes
        from ctypes import wintypes
        import msvcrt

        self.root = root
        self.relative = relative
        self.original = _target_path(root, relative)
        self.path = self.original
        self.moved = False
        self.deleted = False
        self.digest = None
        self.file = None
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                      wintypes.LPVOID, wintypes.DWORD]
        self.api.SetFileInformationByHandle.restype = wintypes.BOOL
        # GENERIC_READ | DELETE, no sharing; OPEN_EXISTING; OPEN_REPARSE_POINT.
        raw = str(self.original.absolute())
        extended = "\\\\?\\UNC\\" + raw[2:] if raw.startswith("\\\\") else "\\\\?\\" + raw
        handle = self.api.CreateFileW(extended, 0x80010000, 0, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise InstallError(f"Cannot lock managed mod file; concurrent access preserved: {relative}: "
                               f"{ctypes.WinError(ctypes.get_last_error())}", target_paths=(relative,))
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            self.api.CloseHandle(handle)
            raise
        try:
            self.file = os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise
        self.handle = msvcrt.get_osfhandle(descriptor)
        try:
            info = os.fstat(descriptor)
        except Exception:
            self.close()
            raise
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            self.close()
            raise InstallError(f"Managed mod file is redirected or not regular: {relative}",
                               target_paths=(relative,))

    def _rename(self, destination: Path) -> None:
        import ctypes
        from ctypes import wintypes

        _directory_path(self.root, destination.parent.relative_to(self.root).as_posix())
        name = str(destination.absolute()).encode("utf-16-le")

        class RenameInfo(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                        ("FileNameLength", wintypes.DWORD), ("FileName", ctypes.c_ushort * 1)]

        buffer = ctypes.create_string_buffer(RenameInfo.FileName.offset + len(name) + 2)
        info = RenameInfo.from_buffer(buffer)
        info.Flags = 0  # ReplaceIfExists=False: never replace a concurrent name.
        info.FileNameLength = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, name, len(name))
        if not self.api.SetFileInformationByHandle(self.handle, 3, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.path = destination

    def capture(self, expected: str) -> None:
        destination = self.original.parent / f".{self.original.name}.w3sub-quarantine-{uuid.uuid4().hex}.tmp"
        self._rename(destination)
        self.moved = True
        self.file.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: self.file.read(1024 * 1024), b""):
            digest.update(block)
        self.digest = digest.hexdigest()
        if self.digest != expected:
            raise InstallError(f"Managed mod bytes changed; quarantined bytes preserved: {self.relative}",
                               target_paths=(self.relative,))

    def restore(self) -> None:
        self._rename(self.original)
        self.moved = False

    def discard(self) -> None:
        import ctypes
        # FILE_DISPOSITION_INFO.DeleteFile=True affects this pinned object.
        disposition = ctypes.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(self.handle, 4, ctypes.byref(disposition), 1):
            raise ctypes.WinError(ctypes.get_last_error())
        self.deleted = True
        self.close()

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


def _sibling_stage(target: Path, source: Path, prefix: str) -> Path:
    stage = target.parent / f".{target.name}.{prefix}-{uuid.uuid4().hex}.tmp"
    _copy_new(source, stage)
    return stage


def _restore_manifest(previous: InstallManifest | None, prepared: InstallManifest,
                      remove_if_missing: bool) -> None:
    if previous is not None:
        _atomic_write_manifest(previous)
    elif remove_if_missing:
        (prepared.state_directory / "install.json").unlink(missing_ok=True)


def _report_rollback_progress(callback: ProgressCallback | None, operation_name: str,
                              completed: int, total: int, *,
                              phase: str = "restoring rollback targets") -> None:
    """An observer failure must not interrupt restoration or mask its diagnostics."""
    try:
        report_progress(callback, f"{operation_name}: {phase}", completed, total)
    except Exception:
        pass


def _state_child_directory(state: Path, parent: Path, name: str, *, exclusive: bool = False) -> Path:
    """Create one state directory only after validating its existing ancestry."""
    state = Path(state)
    if _is_reparse(state.parent) or _is_reparse(state) or not state.is_dir():
        raise InstallError("Game state directory is redirected or unavailable")
    current = state
    for component in parent.relative_to(state).parts:
        current = current / component
        if _is_reparse(current) or not current.is_dir() or not _inside(current, state):
            raise InstallError(f"State parent is redirected or unavailable: {current}")
    destination = parent / name
    try:
        destination.lstat()
    except FileNotFoundError:
        destination.mkdir()
    else:
        if exclusive:
            raise InstallError(f"State directory already exists: {destination}")
    if (_is_reparse(destination) or not destination.is_dir()
            or not _inside(destination.resolve(strict=True), state)):
        raise InstallError(f"State directory is redirected: {destination}")
    return destination


def _apply_transaction(game: GameInstallation,
                       state_directory: Path,
                       prepared_manifest: InstallManifest,
                       final_manifest: InstallManifest,
                       previous_manifest: InstallManifest | None,
                       desired_sources: dict[str, Path | None],
                       desired_hashes: dict[str, str | None],
                       expected_current: dict[str, str | None],
                       *, remove_manifest_on_rollback: bool = False,
                       pre_mutation_check=None,
                       progress_callback: ProgressCallback | None = None,
                       operation_name: str) -> InstallManifest:
    """Commit replacements, exclusive creates and removals as one rollback unit.

    None hashes represent absence. Rollback snapshots preserve the previous
    mod files if publication fails.
    """
    root = _normalized_path(game.root)
    if os.name != "nt" and any(_is_mod_target(relative) for relative in desired_sources):
        raise InstallError("Safe managed-mod transactions require Windows file sharing; no targets changed")
    if set(desired_sources) != set(desired_hashes) or set(desired_sources) != set(expected_current):
        raise InstallError("Transaction path inventory is inconsistent")
    if (any(_is_mod_target(relative) for relative in desired_sources)
            or (previous_manifest is not None and previous_manifest.created_directories)):
        _validate_mod_inventory(root, previous_manifest)
    targets = {relative: _target_path(root, relative, allow_missing=True)
               for relative in sorted(desired_sources)}
    for relative, source in desired_sources.items():
        if (source is None) != (desired_hashes[relative] is None):
            raise InstallError(f"Transaction source inventory is inconsistent: {relative}")
        if (source is None or expected_current[relative] is None) and not _is_mod_target(relative):
            raise InstallError(f"Existing game target cannot be created or removed: {relative}")
        if _current_digest(root, relative) != expected_current[relative]:
            raise InstallError(f"Game target changed before transaction: {relative}",
                               target_paths=(relative,))
        if source is not None and _hash_file(source) != desired_hashes[relative]:
            raise InstallError(f"Transaction source hash changed: {relative}",
                               target_paths=(relative,))

    transaction_directory = state_directory / "transactions" / uuid.uuid4().hex
    snapshots: dict[str, Path] = {}
    quarantines: dict[str, _ModQuarantine] = {}
    rollback_quarantines: list[_ModQuarantine] = []
    published_mod_paths: set[str] = set()
    stages: dict[str, Path] = {}
    attempted: list[str] = []
    restore_stages: list[Path] = []
    created: set[str] = set()
    prior_directories = set(previous_manifest.created_directories) if previous_manifest else set()
    rollback_errors: list[str] = []
    committed = False
    removed_directories: set[str] = set()
    owned_root = previous_manifest is not None and (
        MOD_ROOT in previous_manifest.created_directories
        or any(_is_mod_target(relative) for relative in previous_manifest.target_files))
    try:
        transaction_parent = _state_child_directory(state_directory, state_directory, "transactions")
        _state_child_directory(state_directory, transaction_parent, transaction_directory.name, exclusive=True)
        for number, (relative, target) in enumerate(targets.items(), 1):
            if expected_current[relative] is not None:
                snapshot = transaction_directory.joinpath(*_safe_relative(relative))
                digest = _copy_new(target, snapshot)
                if digest != expected_current[relative]:
                    raise InstallError(f"Game target changed while snapshotting: {relative}",
                                       target_paths=(relative,))
                snapshots[relative] = snapshot
            report_progress(progress_callback, f"{operation_name}: snapshotting targets", number, len(targets))

        _atomic_write_manifest(prepared_manifest)
        for number, (relative, target) in enumerate(targets.items(), 1):
            source = desired_sources[relative]
            if source is not None:
                _create_target_parents(root, relative, created, owned_root=owned_root)
                _target_path(root, relative, allow_missing=True)
                stage = _sibling_stage(target, source, "w3sub-stage")
                stages[relative] = stage
                if _hash_file(stage) != desired_hashes[relative]:
                    raise InstallError(f"Staged replacement hash mismatch: {relative}",
                                       target_paths=(relative,))
            report_progress(progress_callback, f"{operation_name}: staging files", number, len(targets))
        directories = tuple(sorted(prior_directories | created))
        prepared_manifest = replace(prepared_manifest, created_directories=directories)
        final_manifest = replace(final_manifest, created_directories=directories)
        _atomic_write_manifest(prepared_manifest)
        if pre_mutation_check is not None:
            pre_mutation_check()
        else:
            _ensure_game_closed()
        if any(_is_mod_target(relative) for relative in desired_sources) or prior_directories:
            _validate_mod_inventory(root, prepared_manifest, staged_paths=tuple(
                stage for relative, stage in stages.items() if _is_mod_target(relative)))

        for number, (relative, target) in enumerate(targets.items(), 1):
            if _current_digest(root, relative) != expected_current[relative]:
                raise InstallError(f"Game target changed during transaction: {relative}",
                                   target_paths=(relative,))
            if desired_sources[relative] is not None:
                if (_is_reparse(stages[relative])
                        or _hash_file(stages[relative]) != desired_hashes[relative]):
                    raise InstallError(f"Staged replacement changed before publication: {relative}",
                                       target_paths=(relative,))
            # OS errors can be ambiguous about whether a mutation happened.
            attempted.append(relative)
            if _is_mod_target(relative) and expected_current[relative] is not None:
                quarantine = _ModQuarantine(root, relative)
                quarantines[relative] = quarantine
                quarantine.capture(expected_current[relative])
                if desired_sources[relative] is not None:
                    _publish_new(stages[relative], target)
                    published_mod_paths.add(relative)
            elif desired_sources[relative] is None:
                raise InstallError(f"Untracked removal is forbidden: {relative}")
            elif expected_current[relative] is None:
                # Exclusive publication refuses a concurrently created
                # unmanaged file instead of silently replacing it.
                try:
                    _publish_new(stages[relative], target)
                    if _is_mod_target(relative):
                        published_mod_paths.add(relative)
                except FileExistsError as error:
                    attempted.pop()  # Exclusive publication made no change.
                    raise InstallError(f"Unmanaged file appeared before creation: {relative}",
                                       target_paths=(relative,)) from error
            else:
                os.replace(stages[relative], target)
            report_progress(progress_callback, f"{operation_name}: replacing targets", number, len(targets))

        for number, relative in enumerate(targets, 1):
            if _current_digest(root, relative) != desired_hashes[relative]:
                raise InstallError(f"Installed target hash verification failed: {relative}",
                                   target_paths=(relative,))
            report_progress(progress_callback, f"{operation_name}: verifying installed targets", number, len(targets))
        # Dispose only the locked objects whose captured bytes matched. Their
        # verified snapshots remain available if manifest publication fails.
        for quarantine in quarantines.values():
            quarantine.discard()
        removed_directories = _remove_empty_created_directories(root, prior_directories | created)
        # Drop successfully removed and already missing directories so a later
        # user-created directory at the same name is never claimed for cleanup.
        final_manifest = replace(final_manifest, created_directories=tuple(sorted(
            relative for relative in prior_directories | created
            if _directory_path(root, relative).exists())))
        report_progress(progress_callback, f"{operation_name}: saving install manifest", 0, 1)
        _atomic_write_manifest(final_manifest)
        report_progress(progress_callback, f"{operation_name}: saving install manifest", 1, 1)
        committed = True
    except Exception as operation_error:
        for number, relative in enumerate(reversed(attempted), 1):
            target = targets[relative]
            try:
                if _is_mod_target(relative):
                    original = quarantines.get(relative)
                    if original is not None and original.moved:
                        if _current_digest(root, relative) is not None:
                            if relative not in published_mod_paths:
                                raise OSError(f"concurrent file preserved; original retained at {original.path}")
                            current = _ModQuarantine(root, relative)
                            rollback_quarantines.append(current)
                            try:
                                current.capture(desired_hashes[relative])
                            except Exception:
                                if current.moved:
                                    current.restore()
                                raise
                            current.discard()
                        if original.deleted:
                            _create_target_parents(root, relative, created)
                            restore_stage = _sibling_stage(target, snapshots[relative], "w3sub-rollback")
                            restore_stages.append(restore_stage)
                            if _hash_file(restore_stage) != expected_current[relative]:
                                raise OSError("rollback snapshot hash changed")
                            _publish_new(restore_stage, target)
                        else:
                            _create_target_parents(root, relative, created)
                            original.restore()
                            original.close()
                            if original.digest != expected_current[relative]:
                                raise OSError("concurrent original bytes restored and preserved; resolve conflict")
                    elif relative in published_mod_paths:
                        current = _ModQuarantine(root, relative)
                        rollback_quarantines.append(current)
                        try:
                            current.capture(desired_hashes[relative])
                        except Exception:
                            if current.moved:
                                current.restore()
                            raise
                        current.discard()
                    _report_rollback_progress(progress_callback, operation_name, number, len(attempted))
                    continue
                current_hash = _current_digest(root, relative)
                if current_hash != expected_current[relative]:
                    if current_hash != desired_hashes[relative]:
                        raise OSError("unexpected third-party bytes preserved; rollback requires recovery")
                    _create_target_parents(root, relative, created)
                    restore_stage = _sibling_stage(target, snapshots[relative], "w3sub-rollback")
                    restore_stages.append(restore_stage)
                    if _hash_file(restore_stage) != expected_current[relative]:
                        raise OSError("rollback snapshot hash changed")
                    if _current_digest(root, relative) != desired_hashes[relative]:
                        raise OSError("target changed while preparing rollback; unexpected bytes preserved")
                    os.replace(restore_stage, target)
                    if _current_digest(root, relative) != expected_current[relative]:
                        raise OSError("restored hash does not match the transaction snapshot")
                _report_rollback_progress(progress_callback, operation_name, number, len(attempted))
            except Exception as rollback_error:
                preserved = [str(quarantine.path) for quarantine in
                             (*quarantines.values(), *rollback_quarantines)
                             if quarantine.relative == relative and quarantine.moved and not quarantine.deleted]
                detail = f"{relative}: {rollback_error}"
                if preserved:
                    detail += "; preserved quarantines: " + ", ".join(preserved)
                rollback_errors.append(detail)
        for directory in sorted(removed_directories & prior_directories,
                                key=lambda value: (value.count("/"), value)):
            try:
                path = _directory_path(root, directory)
                path.mkdir(exist_ok=True)
                _directory_path(root, directory)
            except (OSError, InstallError) as directory_error:
                rollback_errors.append(f"{directory}: {directory_error}")
        if rollback_errors:
            conflicted = replace(prepared_manifest, active=True, conflicted=True, prepared=True,
                created_directories=tuple(sorted(prior_directories | created)),
                conflict_paths=tuple(sorted(error.split(":", 1)[0] for error in rollback_errors)))
            try:
                _atomic_write_manifest(conflicted)
            except Exception as manifest_error:
                rollback_errors.append(f"install manifest: {manifest_error}")
        else:
            try:
                _restore_manifest(previous_manifest, prepared_manifest, remove_manifest_on_rollback)
            except Exception as manifest_error:
                rollback_errors.append(f"install manifest: {manifest_error}")
        message = str(operation_error) if isinstance(operation_error, InstallError) else f"Install transaction failed: {operation_error}"
        paths = set(attempted)
        if isinstance(operation_error, InstallError):
            paths.update(operation_error.target_paths)
        raise InstallError(message, target_paths=tuple(sorted(paths)), rollback_errors=rollback_errors) from operation_error
    finally:
        for quarantine in (*quarantines.values(), *rollback_quarantines):
            quarantine.close()  # Undiscarded originals remain named for recovery.
        for stage in (*stages.values(), *restore_stages):
            try:
                relative = stage.parent.relative_to(root).as_posix()
                if relative.startswith("Mods/"):
                    _directory_path(root, relative)
                else:
                    # Revalidate every parent of a string-resource stage too.
                    owner = next(key for key, target in targets.items() if target.parent == stage.parent)
                    _target_path(root, owner)
                if not _is_reparse(stage):
                    stage.unlink(missing_ok=True)
            except (OSError, InstallError):
                pass
        if committed or not rollback_errors:
            if not committed:
                _remove_empty_created_directories(root, created - prior_directories)
            try:
                if (_is_reparse(state_directory) or _is_reparse(transaction_directory.parent)
                        or not _inside(transaction_directory.parent, state_directory)):
                    raise InstallError("Transaction cleanup directory is redirected")
                if (transaction_directory.exists() and not _is_reparse(transaction_directory)
                        and _inside(transaction_directory.resolve(strict=True), state_directory)):
                    shutil.rmtree(transaction_directory, ignore_errors=True)
            except (OSError, InstallError):
                pass
    return final_manifest


def install_generation(game: GameInstallation, generation_record: GenerationRecord,
                       state_root: Path, *,
                       progress_callback: ProgressCallback | None = None) -> InstallManifest:
    """Atomically install one verified generation as manager-mod overrides."""
    root = _normalized_path(game.root)
    state = _state_directory(state_root, root)
    state.mkdir(parents=True, exist_ok=True)
    with _OperationLock(state):
        _ensure_game_closed()
        _validate_game_root(game)
        existing = load_install_manifest(state_root, root)
        if existing is not None and (existing.active or existing.prepared):
            raise InstallError("A dual subtitle install is already active; use Modify or Uninstall")
        freshness = _generation_freshness(
            generation_record, game, progress_callback=progress_callback,
        )
        if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(f"Generation is {freshness.value}; regenerate before installing")
        outputs = _verified_outputs(generation_record, game, progress_callback=progress_callback)
        if any(not _is_mod_target(relative) for relative in outputs):
            raise InstallError("Generated outputs must be installed as manager-mod files")
        _validate_mod_inventory(root, None)
        output_hashes = _output_hashes(generation_record)
        install_id = uuid.uuid4().hex
        if outputs:
            report_progress(progress_callback, "Install: preparing manager-mod targets", 0, len(outputs))
        targets = {
            relative: InstallTarget(relative, output_hashes[relative])
            for relative in sorted(outputs)
        }
        for number, relative in enumerate(sorted(outputs), 1):
            if _current_digest(root, relative) is not None:
                raise InstallError(f"Unmanaged mod file already exists: {relative}", target_paths=(relative,))
            report_progress(progress_callback, "Install: preparing manager-mod targets",
                            number, len(outputs))
        final = _make_manifest(game, generation_record, state, install_id, targets, active=True)
        prepared = replace(final, prepared=True)
        desired_hashes = dict(output_hashes)
        expected = {relative: None for relative in outputs}
        final = _apply_transaction(game, state, prepared, final,
                           None, outputs, desired_hashes, expected,
                           remove_manifest_on_rollback=True,
                           pre_mutation_check=lambda: _revalidate_before_mutation(
                               generation_record, game, progress_callback=progress_callback),
                           progress_callback=progress_callback, operation_name="Install")
        return final


def _validate_game_root(game: GameInstallation) -> None:
    root = _normalized_path(game.root)
    if (not root.is_dir() or _is_reparse(Path(game.root)) or _is_reparse(root)
            or not (root / "content").is_dir()):
        raise InstallError(f"Selected game folder is no longer valid: {root}")
    if _path_identity(game.root) != _path_identity(root):
        raise InstallError("Game folder could not be normalized safely")


def _active_conflicts(manifest: InstallManifest, game: GameInstallation, *,
                      progress_callback: ProgressCallback | None = None) -> tuple[str, ...]:
    if not manifest.active:
        return ()
    conflicts = []
    if manifest.created_directories or any(_is_mod_target(relative) for relative in manifest.target_files):
        try:
            _validate_mod_inventory(_normalized_path(game.root), manifest)
        except InstallError as error:
            conflicts.extend(error.target_paths or (MOD_ROOT,))
        except OSError:
            conflicts.append(MOD_ROOT)
    if manifest.target_files:
        report_progress(progress_callback, "Checking managed targets", 0, len(manifest.target_files))
    for number, (relative, target) in enumerate(sorted(manifest.target_files.items()), 1):
        try:
            path = _target_path(game.root, relative)
            if _hash_file(path) != target.installed_sha256:
                conflicts.append(relative)
        except (OSError, InstallError):
            conflicts.append(relative)
        finally:
            report_progress(progress_callback, "Checking managed targets",
                            number, len(manifest.target_files))
    return tuple(sorted(set(conflicts)))


def _freshness(manifest: InstallManifest, game: GameInstallation, *,
               progress_callback: ProgressCallback | None = None) -> Freshness:
    if _path_identity(manifest.game_root) != _path_identity(game.root):
        return Freshness.STALE
    if manifest.storefront is not game.storefront:
        return Freshness.STALE
    try:
        sources = generation._source_paths(
            game, manifest.primary_language, manifest.secondary_language,
        )
        fingerprint = generation._fingerprint_sources(
            sources, progress_callback, "Checking installed source fingerprints",
        )
    except OSError:
        return Freshness.UNREADABLE
    except (generation.GenerationError, ValueError):
        return Freshness.STALE
    if fingerprint != manifest.source_fingerprint:
        return Freshness.STALE
    if manifest.cutscene_bundle_fingerprint is not None:
        if manifest.cutscene_bundle_content_fingerprint is None:
            return Freshness.STALE
        if game.version != manifest.install_version:
            return Freshness.STALE
        try:
            if fingerprint_cutscene_bundles(game) != manifest.cutscene_bundle_fingerprint:
                return Freshness.STALE
        except OSError:
            return Freshness.UNREADABLE
        except ValueError:
            return Freshness.STALE
    if game.version != manifest.install_version:
        return Freshness.VERSION_METADATA_CHANGED_ONLY
    return Freshness.CURRENT


def compare_install(manifest: InstallManifest, game: GameInstallation, *,
                    progress_callback: ProgressCallback | None = None) -> InstallComparison:
    """Compare source/version freshness separately from target hash conflicts."""
    if not isinstance(manifest, InstallManifest):
        return InstallComparison(Freshness.STALE, ())
    if _path_identity(manifest.game_root) != _path_identity(game.root):
        return InstallComparison(Freshness.STALE, ())
    try:
        _validate_manifest(manifest, game.root)
    except InstallError:
        targets = manifest.target_files if isinstance(manifest.target_files, dict) else {}
        paths = tuple(sorted(path for path in targets if isinstance(path, str)))
        return InstallComparison(Freshness.STALE, paths if manifest.active else ())
    return InstallComparison(
        _freshness(manifest, game, progress_callback=progress_callback),
        _active_conflicts(manifest, game, progress_callback=progress_callback),
    )


def _persist_conflicts(manifest: InstallManifest, conflicts: tuple[str, ...]) -> InstallManifest:
    conflicted = replace(manifest, conflicted=bool(conflicts), conflict_paths=conflicts)
    _atomic_write_manifest(conflicted)
    return conflicted


def modify_install(game: GameInstallation, generation_record: GenerationRecord,
                   manifest: InstallManifest, *,
                   progress_callback: ProgressCallback | None = None) -> InstallManifest:
    """Update the manager-mod files to match a verified generation."""
    _validate_manifest(manifest, game.root)
    if manifest.prepared:
        raise InstallError("A previous operation left a prepared manifest; explicit recovery is required")
    if not manifest.active:
        raise InstallError("There is no active dual subtitle install to modify")
    state = _validate_state_directory(manifest.state_directory, game.root)
    with _OperationLock(state):
        _ensure_game_closed()
        comparison = compare_install(manifest, game, progress_callback=progress_callback)
        if comparison.conflict_paths:
            _persist_conflicts(manifest, comparison.conflict_paths)
            raise InstallError("Managed game files were edited; resolve conflicts before Modify",
                               target_paths=comparison.conflict_paths)
        if comparison.freshness not in (Freshness.CURRENT,
                                        Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(
                f"Installed source files are {comparison.freshness.value}; "
                "uninstall safely and regenerate before Modify"
            )
        _validate_game_root(game)
        freshness = _generation_freshness(
            generation_record, game, progress_callback=progress_callback,
        )
        if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(f"Generation is {freshness.value}; regenerate before Modify")
        outputs = _verified_outputs(generation_record, game, progress_callback=progress_callback)
        if any(not _is_mod_target(relative) for relative in outputs):
            raise InstallError("Generated outputs must be installed as manager-mod files")
        output_hashes = _output_hashes(generation_record)
        _validate_mod_inventory(_normalized_path(game.root), manifest)
        output_paths = set(outputs)
        old_paths = set(manifest.target_files)
        union = output_paths | old_paths
        final_targets = {
            relative: InstallTarget(relative, output_hashes[relative])
            for relative in sorted(output_paths)
        }
        final = _make_manifest(
            game, generation_record, state, manifest.install_id, final_targets, active=True,
        )
        prepared_targets = dict(manifest.target_files)
        prepared_targets.update(final_targets)
        prepared = replace(final, target_files=prepared_targets, prepared=True,
            cutscene_bundle_fingerprint=(final.cutscene_bundle_fingerprint
                                         or manifest.cutscene_bundle_fingerprint),
            cutscene_bundle_content_fingerprint=(final.cutscene_bundle_content_fingerprint
                                                 or manifest.cutscene_bundle_content_fingerprint))

        desired_sources = dict(outputs)
        desired_hashes = dict(output_hashes)
        expected = {}
        if union:
            report_progress(progress_callback, "Modify: preparing managed files", 0, len(union))
        for number, relative in enumerate(sorted(union), 1):
            previous = manifest.target_files.get(relative)
            if previous is not None:
                expected[relative] = previous.installed_sha256
            else:
                expected[relative] = None
            if relative not in outputs:
                desired_sources[relative] = None
                desired_hashes[relative] = None
            report_progress(progress_callback, "Modify: preparing managed files", number, len(union))
        final = _apply_transaction(game, state,
                           prepared, final, manifest,
                           desired_sources, desired_hashes, expected,
                           pre_mutation_check=lambda: _revalidate_before_mutation(
                               generation_record, game, progress_callback=progress_callback),
                           progress_callback=progress_callback, operation_name="Modify")
        return final


def uninstall(game: GameInstallation, manifest: InstallManifest, *,
              progress_callback: ProgressCallback | None = None) -> UninstallResult:
    """Remove manager-mod files installed by this manifest."""
    _validate_manifest(manifest, game.root)
    if manifest.prepared:
        raise InstallError("A previous operation left a prepared manifest; explicit recovery is required")
    if not manifest.active:
        return UninstallResult(())
    state = _validate_state_directory(manifest.state_directory, game.root)
    with _OperationLock(state):
        _ensure_game_closed()
        conflicts = _active_conflicts(manifest, game, progress_callback=progress_callback)
        if conflicts:
            _persist_conflicts(manifest, conflicts)
            return UninstallResult((), conflicts)
        sources = {}
        hashes = {}
        expected = {}
        if manifest.target_files:
            report_progress(progress_callback, "Uninstall: preparing managed files", 0, len(manifest.target_files))
        for number, (relative, target) in enumerate(sorted(manifest.target_files.items()), 1):
            sources[relative] = None
            hashes[relative] = None
            expected[relative] = target.installed_sha256
            report_progress(progress_callback, "Uninstall: preparing managed files",
                            number, len(manifest.target_files))
        prepared = replace(manifest, schema_version=INSTALL_MANIFEST_SCHEMA, conflicted=False, conflict_paths=(), prepared=True)
        final = replace(manifest, schema_version=INSTALL_MANIFEST_SCHEMA, active=False, conflicted=False,
                        conflict_paths=(), prepared=False)
        try:
            _apply_transaction(game, state,
                               prepared, final, manifest,
                               sources, hashes, expected,
                               progress_callback=progress_callback, operation_name="Uninstall")
        except InstallError as error:
            failed_paths = {
                relative for relative in sorted(manifest.target_files)
                if any(detail.startswith(f"{relative}:")
                       for detail in error.rollback_errors)
            }
            for relative in error.target_paths:
                target = manifest.target_files.get(relative)
                if target is None:
                    failed_paths.add(relative)
                    continue
                try:
                    if _current_digest(game.root, relative) != target.installed_sha256:
                        failed_paths.add(relative)
                except (OSError, InstallError):
                    failed_paths.add(relative)
            return UninstallResult(
                (), tuple(sorted(failed_paths)), error.rollback_errors, str(error),
            )
        return UninstallResult(tuple(sorted(sources)))
