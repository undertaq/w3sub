"""Transactional install lifecycle for generated Witcher 3 string resources."""
import csv
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import shutil
import stat
import uuid

from . import generation
from .models import (
    Freshness,
    GameInstallation,
    GameVersion,
    GenerationRecord,
    InstallComparison,
    InstallManifest,
    InstallTarget,
    MergeMode,
    ResourceFingerprint,
    Storefront,
    UninstallResult,
)


INSTALL_MANIFEST_SCHEMA = 1


class InstallError(RuntimeError):
    """An install operation failed without hiding rollback diagnostics."""

    def __init__(self, message, *, target_paths=(), rollback_errors=(), backup_directory=None):
        super().__init__(message)
        self.target_paths = tuple(target_paths)
        self.rollback_errors = tuple(rollback_errors)
        self.backup_directory = backup_directory


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
        raise InstallError("Install state and backups must stay outside the game folder")
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


def _safe_relative(relative: str) -> tuple[str, ...]:
    parts = generation._safe_relative_output(relative)
    if (parts is None or len(parts) < 2
            or parts[0].casefold() not in {"content", "dlc"}
            or PurePosixPath(relative).suffix.casefold() != ".w3strings"):
        raise InstallError(f"Unsafe game target path: {relative!r}")
    return parts


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


def _target_path(game_root: Path, relative: str) -> Path:
    parts = _safe_relative(relative)
    root = _normalized_path(game_root)
    if not root.is_dir():
        raise InstallError(f"Game folder is unavailable: {root}")
    candidate = root.joinpath(*parts)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            if _is_reparse(current):
                raise InstallError(f"Game target contains a symbolic link or junction: {relative}")
            resolved = current.resolve(strict=True)
        except OSError as error:
            raise InstallError(f"Cannot access game target {relative}: {error}") from error
        if not _inside(resolved, root):
            raise InstallError(f"Game target resolves outside the selected folder: {relative}")
        if index < len(parts) - 1 and not resolved.is_dir():
            raise InstallError(f"Game target parent is not a directory: {relative}")
    try:
        if not candidate.is_file():
            raise InstallError(f"Game target is not a regular file: {relative}")
    except OSError as error:
        raise InstallError(f"Cannot inspect game target {relative}: {error}") from error
    return candidate


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
        "backup_directory": str(manifest.backup_directory),
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
        "target_files": {
            relative: {
                "relative_path": target.relative_path,
                "backup_path": str(target.backup_path),
                "original_sha256": target.original_sha256,
                "installed_sha256": target.installed_sha256,
            }
            for relative, target in sorted(manifest.target_files.items())
        },
        "active": manifest.active,
        "conflicted": manifest.conflicted,
        "conflict_paths": list(manifest.conflict_paths),
        "prepared": manifest.prepared,
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
    if payload.get("schema_version") != INSTALL_MANIFEST_SCHEMA:
        raise ValueError("unsupported install manifest schema")
    for name in ("active", "conflicted", "prepared"):
        if name in payload and not isinstance(payload[name], bool):
            raise ValueError(f"{name} must be a boolean")
    if not isinstance(payload.get("target_files"), dict):
        raise ValueError("target_files must be an object")
    if not isinstance(payload.get("conflict_paths", []), list) or any(
            not isinstance(path, str) for path in payload.get("conflict_paths", [])):
        raise ValueError("conflict_paths must be a list of paths")
    raw_fingerprint = payload["source_fingerprint"]
    if not isinstance(raw_fingerprint, dict) or not isinstance(raw_fingerprint.get("entries"), dict):
        raise ValueError("source_fingerprint must contain an entries object")
    fingerprint = ResourceFingerprint(dict(raw_fingerprint["entries"]), raw_fingerprint["digest"])
    targets = {}
    for relative, entry in payload["target_files"].items():
        if entry["relative_path"] != relative:
            raise ValueError(f"target path key mismatch: {relative}")
        targets[relative] = InstallTarget(
            relative_path=relative,
            backup_path=Path(entry["backup_path"]),
            original_sha256=entry["original_sha256"],
            installed_sha256=entry["installed_sha256"],
        )
    return InstallManifest(
        schema_version=payload["schema_version"],
        install_id=payload["install_id"],
        game_root=Path(payload["game_root"]),
        state_directory=Path(payload["state_directory"]),
        backup_directory=Path(payload["backup_directory"]),
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
    )


def _atomic_write_manifest(manifest: InstallManifest) -> None:
    state = _validate_state_directory(manifest.state_directory, manifest.game_root)
    state.mkdir(parents=True, exist_ok=True)
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
                           target_paths=manifest.target_files,
                           backup_directory=manifest.backup_directory) from error


def _validate_manifest(manifest: InstallManifest, game_root: Path) -> None:
    if not isinstance(manifest, InstallManifest):
        raise InstallError("Install manifest has an unsupported type")
    root = _normalized_path(game_root)
    if _path_identity(manifest.game_root) != _path_identity(root):
        raise InstallError("Install manifest belongs to a different game folder")
    state = _validate_state_directory(manifest.state_directory, root)
    expected_backup = state / "backups" / manifest.install_id
    if _normalized_path(manifest.backup_directory) != expected_backup.resolve():
        raise InstallError("Install backup path does not match its game-state location")
    if (not isinstance(manifest.install_id, str) or len(manifest.install_id) != 32
            or any(character not in "0123456789abcdef" for character in manifest.install_id)):
        raise InstallError("Install manifest has an invalid install id")
    if any(not isinstance(value, bool) for value in
           (manifest.active, manifest.conflicted, manifest.prepared)):
        raise InstallError("Install manifest contains invalid lifecycle flags")
    if not isinstance(manifest.target_files, dict):
        raise InstallError("Install manifest target inventory is invalid")
    if any(not isinstance(target, InstallTarget)
           for target in manifest.target_files.values()):
        raise InstallError("Install manifest target record is invalid")
    if set(manifest.target_files) != {target.relative_path for target in manifest.target_files.values()}:
        raise InstallError("Install manifest contains inconsistent target paths")
    for relative, target in manifest.target_files.items():
        _safe_relative(relative)
        if (not _is_digest(target.original_sha256)
                or not _is_digest(target.installed_sha256)):
            raise InstallError(f"Install manifest has an invalid hash for {relative}")
        expected = expected_backup.joinpath(*_safe_relative(relative))
        if _normalized_path(target.backup_path) != expected.resolve():
            raise InstallError(f"Install backup path is invalid for {relative}")


def _is_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value.lower()))


def _load_manifest_file(path: Path, game_root: Path | None = None) -> InstallManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = _manifest_from_payload(payload)
        _validate_manifest(manifest, game_root or manifest.game_root)
        expected_path = manifest.state_directory / "install.json"
        if _normalized_path(path) != _normalized_path(expected_path):
            raise ValueError("manifest is stored outside its declared state directory")
        return manifest
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
    try:
        with Path(source).open("rb") as input_file, destination.open("xb") as output_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
    except OSError:
        destination.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _backup_target(root: Path, backup_directory: Path, relative: str) -> InstallTarget:
    target = _target_path(root, relative)
    backup = backup_directory.joinpath(*_safe_relative(relative))
    digest = _copy_new(target, backup)
    if _hash_file(target) != digest or _hash_file(backup) != digest:
        raise InstallError(f"Game target changed while it was being backed up: {relative}")
    return InstallTarget(relative, backup, digest, "0" * 64)


def _validate_backup(manifest: InstallManifest, relative: str, target: InstallTarget) -> Path:
    parts = _safe_relative(relative)
    state = _validate_state_directory(manifest.state_directory, manifest.game_root)
    backup_directory = Path(manifest.backup_directory)
    expected_directory = state / "backups" / manifest.install_id
    try:
        if _normalized_path(backup_directory) != expected_directory.resolve(strict=True):
            raise InstallError("Backup directory does not match the manifest location")
        if _is_reparse(backup_directory) or not backup_directory.is_dir():
            raise InstallError("Backup directory is unavailable or redirected")
        backup = Path(target.backup_path)
        expected = backup_directory.joinpath(*parts)
        if _normalized_path(backup) != expected.resolve(strict=True):
            raise InstallError(f"Backup path is invalid for {relative}")
        current = backup_directory
        for part in parts:
            current = current / part
            if _is_reparse(current):
                raise InstallError(f"Original backup path is redirected for {relative}")
            if not _inside(current.resolve(strict=True), backup_directory):
                raise InstallError(f"Original backup escapes its backup directory: {relative}")
        if not backup.is_file():
            raise InstallError(f"Original backup is unavailable for {relative}")
        if _hash_file(backup) != target.original_sha256:
            raise InstallError(f"Original backup hash does not match manifest for {relative}")
        return backup
    except OSError as error:
        raise InstallError(f"Cannot validate original backup for {relative}: {error}") from error


def _generation_overrides(manifest: InstallManifest,
                          record: GenerationRecord) -> dict[str, Path]:
    overrides = {}
    for relative in record.source_fingerprint.entries:
        target = manifest.target_files.get(relative)
        if target is not None:
            overrides[relative] = _validate_backup(manifest, relative, target)
    return overrides


def _generation_freshness(record: GenerationRecord, game: GameInstallation,
                          overrides: dict[str, Path] | None = None) -> Freshness:
    if _path_identity(record.game_root) != _path_identity(game.root):
        return Freshness.STALE
    return generation.compare_generation(record, game, overrides)


def _verified_outputs(record: GenerationRecord, game: GameInstallation) -> dict[str, Path]:
    if not generation._verify_outputs(record, game.root):
        raise InstallError("Generation output inventory or hash is invalid")
    outputs = {}
    for relative, raw in record.output_files.items():
        _safe_relative(relative)
        target = _target_path(game.root, relative)
        source = Path(raw)
        if _is_reparse(source) or not source.is_file():
            raise InstallError(f"Generated output is unavailable: {relative}")
        if _hash_file(source) != record.output_hashes[relative]:
            raise InstallError(f"Generated output changed after generation: {relative}")
        outputs[relative] = source
    return outputs


def _make_manifest(game: GameInstallation, record: GenerationRecord,
                   state_directory: Path, backup_directory: Path,
                   install_id: str, targets: dict[str, InstallTarget], *,
                   active: bool, conflicted: bool = False,
                   conflict_paths: tuple[str, ...] = (),
                   prepared: bool = False) -> InstallManifest:
    return InstallManifest(
        schema_version=INSTALL_MANIFEST_SCHEMA,
        install_id=install_id,
        game_root=_normalized_path(game.root),
        state_directory=state_directory,
        backup_directory=backup_directory,
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
    )


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


def _apply_transaction(game: GameInstallation,
                       state_directory: Path,
                       backup_directory: Path,
                       prepared_manifest: InstallManifest,
                       final_manifest: InstallManifest,
                       previous_manifest: InstallManifest | None,
                       desired_sources: dict[str, Path],
                       desired_hashes: dict[str, str],
                       expected_current: dict[str, str],
                       *, remove_manifest_on_rollback: bool = False) -> None:
    root = _normalized_path(game.root)
    if set(desired_sources) != set(desired_hashes) or set(desired_sources) != set(expected_current):
        raise InstallError("Transaction path inventory is inconsistent")
    targets = {relative: _target_path(root, relative) for relative in sorted(desired_sources)}
    for relative, path in targets.items():
        if _hash_file(path) != expected_current[relative]:
            raise InstallError(f"Game target changed before transaction: {relative}",
                               target_paths=(relative,), backup_directory=backup_directory)
        if _hash_file(desired_sources[relative]) != desired_hashes[relative]:
            raise InstallError(f"Transaction source hash changed: {relative}",
                               target_paths=(relative,), backup_directory=backup_directory)

    transaction_directory = state_directory / "transactions" / uuid.uuid4().hex
    snapshots: dict[str, Path] = {}
    stages: dict[str, Path] = {}
    attempted: list[str] = []
    try:
        transaction_directory.mkdir(parents=True, exist_ok=False)
        for relative, target in targets.items():
            snapshot = transaction_directory.joinpath(*_safe_relative(relative))
            digest = _copy_new(target, snapshot)
            if digest != expected_current[relative]:
                raise InstallError(f"Game target changed while snapshotting: {relative}",
                                   target_paths=(relative,), backup_directory=backup_directory)
            snapshots[relative] = snapshot

        _atomic_write_manifest(prepared_manifest)
        for relative, target in targets.items():
            stages[relative] = _sibling_stage(target, desired_sources[relative], "w3sub-stage")
            if _hash_file(stages[relative]) != desired_hashes[relative]:
                raise InstallError(f"Staged replacement hash mismatch: {relative}",
                                   target_paths=(relative,), backup_directory=backup_directory)

        for relative, target in targets.items():
            if _hash_file(target) != expected_current[relative]:
                raise InstallError(f"Game target changed during transaction: {relative}",
                                   target_paths=(relative,), backup_directory=backup_directory)
            # Treat a replacement as attempted before calling the OS: an error
            # can be ambiguous about whether the filesystem changed the name.
            attempted.append(relative)
            try:
                os.replace(stages[relative], target)
            except OSError as error:
                raise InstallError(
                    f"Cannot replace {relative}; close the game if it is running: {error}",
                    target_paths=(relative,), backup_directory=backup_directory,
                ) from error

        for relative, target in targets.items():
            if _hash_file(target) != desired_hashes[relative]:
                raise InstallError(f"Installed target hash verification failed: {relative}",
                                   target_paths=(relative,), backup_directory=backup_directory)
        _atomic_write_manifest(final_manifest)
    except Exception as operation_error:
        rollback_errors = []
        for relative in reversed(attempted):
            target = targets[relative]
            try:
                if _hash_file(target) == expected_current[relative]:
                    continue
                restore_stage = _sibling_stage(target, snapshots[relative], "w3sub-rollback")
                os.replace(restore_stage, target)
                if _hash_file(target) != expected_current[relative]:
                    raise OSError("restored hash does not match the transaction snapshot")
            except Exception as rollback_error:
                rollback_errors.append(f"{relative}: {rollback_error}")
        if rollback_errors:
            conflicted = replace(
                prepared_manifest,
                active=True,
                conflicted=True,
                conflict_paths=tuple(sorted(error.split(":", 1)[0] for error in rollback_errors)),
                prepared=True,
            )
            try:
                _atomic_write_manifest(conflicted)
            except Exception as manifest_error:
                rollback_errors.append(f"install manifest: {manifest_error}")
        else:
            try:
                _restore_manifest(previous_manifest, prepared_manifest,
                                  remove_manifest_on_rollback)
            except Exception as manifest_error:
                rollback_errors.append(f"install manifest: {manifest_error}")
        paths = tuple(sorted(set(attempted)))
        if isinstance(operation_error, InstallError):
            message = str(operation_error)
        else:
            message = f"Install transaction failed: {operation_error}"
        raise InstallError(message, target_paths=paths,
                           rollback_errors=rollback_errors,
                           backup_directory=backup_directory) from operation_error
    finally:
        for stage in stages.values():
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
        if not any(error for error in locals().get("rollback_errors", ())):
            shutil.rmtree(transaction_directory, ignore_errors=True)


def install_generation(game: GameInstallation, generation_record: GenerationRecord,
                       state_root: Path) -> InstallManifest:
    """Back up originals and atomically install one verified generation."""
    root = _normalized_path(game.root)
    state = _state_directory(state_root, root)
    state.mkdir(parents=True, exist_ok=True)
    with _OperationLock(state):
        _ensure_game_closed()
        _validate_game_root(game)
        existing = load_install_manifest(state_root, root)
        if existing is not None and (existing.active or existing.prepared):
            raise InstallError("A dual subtitle install is already active; use Modify or Uninstall")
        freshness = _generation_freshness(generation_record, game)
        if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(f"Generation is {freshness.value}; regenerate before installing")
        outputs = _verified_outputs(generation_record, game)
        install_id = uuid.uuid4().hex
        backup_directory = state / "backups" / install_id
        try:
            backup_directory.mkdir(parents=True, exist_ok=False)
            targets = {
                relative: _backup_target(root, backup_directory, relative)
                for relative in sorted(outputs)
            }
        except Exception as error:
            if isinstance(error, InstallError):
                raise
            raise InstallError(f"Cannot back up game originals: {error}",
                               backup_directory=backup_directory) from error
        targets = {
            relative: replace(target, installed_sha256=generation_record.output_hashes[relative])
            for relative, target in targets.items()
        }
        final = _make_manifest(game, generation_record, state, backup_directory,
                               install_id, targets, active=True)
        prepared = replace(final, prepared=True)
        desired_hashes = dict(generation_record.output_hashes)
        expected = {relative: targets[relative].original_sha256 for relative in outputs}
        _apply_transaction(game, state, backup_directory, prepared, final,
                           None, outputs, desired_hashes, expected,
                           remove_manifest_on_rollback=True)
        return final


def _validate_game_root(game: GameInstallation) -> None:
    root = _normalized_path(game.root)
    if not root.is_dir() or not (root / "content").is_dir():
        raise InstallError(f"Selected game folder is no longer valid: {root}")
    if _path_identity(game.root) != _path_identity(root):
        raise InstallError("Game folder could not be normalized safely")


def _active_conflicts(manifest: InstallManifest, game: GameInstallation) -> tuple[str, ...]:
    if not manifest.active:
        return ()
    conflicts = []
    for relative, target in sorted(manifest.target_files.items()):
        try:
            path = _target_path(game.root, relative)
            if _hash_file(path) != target.installed_sha256:
                conflicts.append(relative)
                continue
            try:
                _validate_backup(manifest, relative, target)
            except InstallError:
                conflicts.append(relative)
        except (OSError, InstallError):
            conflicts.append(relative)
    return tuple(conflicts)


def _freshness(manifest: InstallManifest, game: GameInstallation) -> Freshness:
    if _path_identity(manifest.game_root) != _path_identity(game.root):
        return Freshness.STALE
    if manifest.storefront is not game.storefront:
        return Freshness.STALE
    overrides = {}
    if manifest.active:
        for relative in manifest.source_fingerprint.entries:
            target = manifest.target_files.get(relative)
            if target is not None:
                try:
                    overrides[relative] = _validate_backup(manifest, relative, target)
                except InstallError:
                    return Freshness.STALE
    try:
        sources = generation._source_paths(
            game, manifest.primary_language, manifest.secondary_language,
            overrides or None,
        )
        fingerprint = generation._fingerprint_sources(sources)
    except OSError:
        return Freshness.UNREADABLE
    except (generation.GenerationError, ValueError):
        return Freshness.STALE
    if fingerprint != manifest.source_fingerprint:
        return Freshness.STALE
    if manifest.mode is MergeMode.DIALOGUE_ONLY:
        try:
            index = generation._current_dialogue_index(game)
        except OSError:
            return Freshness.UNREADABLE
        if index is None or index.digest != manifest.classifier_digest:
            return Freshness.STALE
    elif manifest.classifier_digest is not None:
        return Freshness.STALE
    if game.version != manifest.install_version:
        return Freshness.VERSION_METADATA_CHANGED_ONLY
    return Freshness.CURRENT


def compare_install(manifest: InstallManifest, game: GameInstallation) -> InstallComparison:
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
    return InstallComparison(_freshness(manifest, game), _active_conflicts(manifest, game))


def _persist_conflicts(manifest: InstallManifest, conflicts: tuple[str, ...]) -> InstallManifest:
    conflicted = replace(manifest, conflicted=bool(conflicts), conflict_paths=conflicts)
    _atomic_write_manifest(conflicted)
    return conflicted


def _copy_new_backups(manifest: InstallManifest, game: GameInstallation,
                      relative_paths: set[str]) -> dict[str, InstallTarget]:
    targets = dict(manifest.target_files)
    for relative in sorted(relative_paths):
        if relative in targets:
            _validate_backup(manifest, relative, targets[relative])
            continue
        path = _target_path(game.root, relative)
        backup = manifest.backup_directory.joinpath(*_safe_relative(relative))
        if backup.exists():
            if _is_reparse(backup) or not backup.is_file():
                raise InstallError(f"Unexpected file blocks a new original backup: {relative}")
            digest = _hash_file(backup)
        else:
            digest = _copy_new(path, backup)
        if digest != _hash_file(path) or digest != _hash_file(backup):
            raise InstallError(f"New target changed while being backed up: {relative}")
        targets[relative] = InstallTarget(relative, backup, digest, "0" * 64)
    return targets


def modify_install(game: GameInstallation, generation_record: GenerationRecord,
                   manifest: InstallManifest) -> InstallManifest:
    """Modify the active pair using originals from its persistent backups."""
    _validate_manifest(manifest, game.root)
    if manifest.prepared:
        raise InstallError("A previous operation left a prepared manifest; explicit recovery is required")
    if not manifest.active:
        raise InstallError("There is no active dual subtitle install to modify")
    state = _validate_state_directory(manifest.state_directory, game.root)
    with _OperationLock(state):
        _ensure_game_closed()
        comparison = compare_install(manifest, game)
        if comparison.conflict_paths:
            _persist_conflicts(manifest, comparison.conflict_paths)
            raise InstallError("Managed game files were edited; resolve conflicts before Modify",
                               target_paths=comparison.conflict_paths,
                               backup_directory=manifest.backup_directory)
        if comparison.freshness not in (Freshness.CURRENT,
                                        Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(
                f"Installed source files are {comparison.freshness.value}; "
                "uninstall safely and regenerate before Modify"
            )
        _validate_game_root(game)
        overrides = _generation_overrides(manifest, generation_record)
        freshness = _generation_freshness(generation_record, game, overrides)
        if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
            raise InstallError(f"Generation is {freshness.value}; regenerate before Modify")
        outputs = _verified_outputs(generation_record, game)
        output_paths = set(outputs)
        old_paths = set(manifest.target_files)
        union = output_paths | old_paths
        all_targets = _copy_new_backups(manifest, game, output_paths)
        final_targets = {
            relative: replace(all_targets[relative],
                              installed_sha256=generation_record.output_hashes[relative])
            for relative in sorted(output_paths)
        }
        final = _make_manifest(
            game, generation_record, state, manifest.backup_directory,
            manifest.install_id, final_targets, active=True,
        )
        prepared_targets = dict(manifest.target_files)
        prepared_targets.update(final_targets)
        prepared = replace(final, target_files=prepared_targets, prepared=True)

        desired_sources = dict(outputs)
        desired_hashes = dict(generation_record.output_hashes)
        expected = {}
        for relative in sorted(union):
            previous = manifest.target_files.get(relative)
            if previous is not None:
                expected[relative] = previous.installed_sha256
            else:
                expected[relative] = all_targets[relative].original_sha256
            if relative not in outputs:
                original = _validate_backup(manifest, relative, previous)
                desired_sources[relative] = original
                desired_hashes[relative] = previous.original_sha256
        _apply_transaction(game, state, manifest.backup_directory,
                           prepared, final, manifest,
                           desired_sources, desired_hashes, expected)
        return final


def uninstall(game: GameInstallation, manifest: InstallManifest) -> UninstallResult:
    """Restore exact originals unless any managed file has been externally edited."""
    _validate_manifest(manifest, game.root)
    if manifest.prepared:
        raise InstallError("A previous operation left a prepared manifest; explicit recovery is required")
    if not manifest.active:
        return UninstallResult((), manifest.backup_directory)
    state = _validate_state_directory(manifest.state_directory, game.root)
    with _OperationLock(state):
        _ensure_game_closed()
        conflicts = _active_conflicts(manifest, game)
        if conflicts:
            _persist_conflicts(manifest, conflicts)
            return UninstallResult((), manifest.backup_directory, conflicts, ())
        sources = {}
        hashes = {}
        expected = {}
        for relative, target in sorted(manifest.target_files.items()):
            sources[relative] = _validate_backup(manifest, relative, target)
            hashes[relative] = target.original_sha256
            expected[relative] = target.installed_sha256
        prepared = replace(manifest, conflicted=False, conflict_paths=(), prepared=True)
        final = replace(manifest, active=False, conflicted=False,
                        conflict_paths=(), prepared=False)
        try:
            _apply_transaction(game, state, manifest.backup_directory,
                               prepared, final, manifest,
                               sources, hashes, expected)
        except InstallError as error:
            failed_paths = tuple(
                relative for relative in sorted(manifest.target_files)
                if any(detail.startswith(f"{relative}:")
                       for detail in error.rollback_errors)
            )
            return UninstallResult(
                (), manifest.backup_directory, failed_paths,
                error.rollback_errors, str(error),
            )
        return UninstallResult(tuple(sorted(sources)), manifest.backup_directory)
