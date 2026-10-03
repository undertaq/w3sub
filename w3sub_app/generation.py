"""Stage dual-language resources and retain the inputs needed to audit them."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import uuid

from .dialogue_index import load_dialogue_index
from .merge import MergeError, merge_csv
from .models import (
    Freshness,
    GameInstallation,
    GameVersion,
    GenerationRecord,
    GenerationRequest,
    MergeMode,
    ResourceFingerprint,
    Storefront,
)


APP_VERSION = "0.1.0"
GENERATION_RECORD_SCHEMA = 1


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


def _normalize_override_key(raw_key: str) -> str:
    if not isinstance(raw_key, str) or not raw_key:
        raise GenerationError("Source override keys must be game-relative resource paths")
    posix = PurePosixPath(raw_key.replace("\\", "/"))
    windows = PureWindowsPath(raw_key)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in posix.parts or not posix.parts or "." in posix.parts):
        raise GenerationError(f"Unsafe source override path: {raw_key!r}")
    return posix.as_posix()


def _source_paths(game: GameInstallation, primary_language: str,
                  secondary_language: str,
                  source_overrides: dict[str, Path] | None) -> dict[str, Path]:
    root = _normalized_game_root(game.root)
    pairs = _pair_resources(game, primary_language, secondary_language)
    primary_resources = _resource_map(game, primary_language)
    secondary_resources = _resource_map(game, secondary_language)
    sources = {}
    for _, primary_rel, secondary_rel in pairs:
        sources[primary_rel] = primary_resources[primary_rel]
        sources[secondary_rel] = secondary_resources[secondary_rel]

    if source_overrides is not None:
        overrides = {}
        for raw_key, raw_path in source_overrides.items():
            key = _normalize_override_key(raw_key)
            if key in overrides:
                raise GenerationError(f"Duplicate source override path: {key}")
            overrides[key] = Path(raw_path)
        unexpected = sorted(overrides.keys() - sources.keys())
        if unexpected:
            raise GenerationError(
                f"Source overrides must target selected language resources: {unexpected}"
            )
        for relative, source in overrides.items():
            # The physical backup may live outside the game; its fingerprint
            # retains the logical game-relative target path.
            sources[relative] = source
    return sources


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint_sources(sources: dict[str, Path]) -> ResourceFingerprint:
    entries = {}
    aggregate = hashlib.sha256()
    for relative, path in sorted(sources.items()):
        digest = _hash_file(path)
        entries[relative] = digest
        encoded = relative.encode("utf-8")
        aggregate.update(len(encoded).to_bytes(8, "big"))
        aggregate.update(encoded)
        aggregate.update(bytes.fromhex(digest))
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


def _current_dialogue_index(game: GameInstallation):
    index = load_dialogue_index(game)
    if (getattr(index, "validated", False) is not True
            or not callable(getattr(index, "is_current", None))
            or not callable(getattr(index, "context_for", None))):
        return None
    try:
        if index.is_current(game) is not True:
            return None
    except Exception:
        return None
    digest = getattr(index, "digest", None)
    if not isinstance(digest, str) or not digest:
        return None
    return index


def _version_payload(version: GameVersion) -> dict[str, str | None]:
    return {
        "executable_version": version.executable_version,
        "executable_version_raw": version.executable_version_raw,
        "store_build_id": version.store_build_id,
    }


def _fingerprint_payload(fingerprint: ResourceFingerprint) -> dict[str, object]:
    return {"entries": dict(sorted(fingerprint.entries.items())), "digest": fingerprint.digest}


def _record_payload(record: GenerationRecord) -> dict[str, object]:
    return {
        "schema_version": GENERATION_RECORD_SCHEMA,
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
        "converter_path": record.converter_path,
        "converter_sha256": record.converter_sha256,
        "converter_version": record.converter_version,
        "app_version": record.app_version,
        "output_files": dict(sorted(record.output_files.items())),
        "output_hashes": dict(sorted(record.output_hashes.items())),
    }


def generate(request: GenerationRequest, state_root: Path, converter) -> GenerationRecord:
    """Generate into per-game app state and atomically publish its JSON record."""
    if not isinstance(request.mode, MergeMode):
        raise GenerationError(f"Unsupported merge mode: {request.mode!r}")
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
    sources = _source_paths(request.game, primary_language, secondary_language,
                            request.source_overrides)
    try:
        initial_fingerprint = _fingerprint_sources(sources)
    except (OSError, ValueError) as error:
        raise GenerationError(f"Cannot fingerprint generation inputs: {error}") from error

    dialogue_index = None
    classifier_digest = None
    if request.mode is MergeMode.DIALOGUE_ONLY:
        dialogue_index = _current_dialogue_index(request.game)
        if dialogue_index is None:
            raise GenerationError(
                "Dialogue-only generation requires a current validated dialogue index"
            )
        classifier_digest = dialogue_index.digest

    converter_path, converter_digest = _hash_executable(converter)
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

    try:
        with tempfile.TemporaryDirectory(prefix="convert-", dir=generations_root) as scratch_name:
            scratch = Path(scratch_name)
            for number, (_, primary_relative, secondary_relative) in enumerate(pairs):
                primary_csv = converter.decode(sources[primary_relative],
                                               scratch / f"{number}-primary")
                secondary_csv = converter.decode(sources[secondary_relative],
                                                 scratch / f"{number}-secondary")
                try:
                    merged_csv = merge_csv(
                        primary_csv,
                        secondary_csv,
                        request.mode,
                        dialogue_index=dialogue_index,
                        current_game=request.game,
                    )
                except MergeError as error:
                    raise GenerationError(f"Cannot merge {primary_relative}: {error}") from error
                encoded = converter.encode(merged_csv, scratch / f"{number}-encoded")
                target = generation_dir.joinpath(*PurePosixPath(primary_relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with Path(encoded).open("rb") as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(chunk)
                output_files[primary_relative] = str(target.resolve())
                output_hashes[primary_relative] = _hash_file(target)

        try:
            final_fingerprint = _fingerprint_sources(sources)
        except (OSError, ValueError) as error:
            raise GenerationError(f"Cannot recheck generation inputs: {error}") from error
        if final_fingerprint != initial_fingerprint:
            raise GenerationError("A language resource changed during generation; retry")
        if request.mode is MergeMode.DIALOGUE_ONLY:
            current_index = _current_dialogue_index(request.game)
            if current_index is None or current_index.digest != classifier_digest:
                raise GenerationError(
                    "The dialogue index changed during generation; retry with a current index"
                )
        current_converter_path, final_converter_digest = _hash_executable(converter)
        if current_converter_path != converter_path or final_converter_digest != converter_digest:
            raise GenerationError("Converter executable changed during generation; retry")

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
        )
        temporary_record = generation_dir / "generation.json.tmp"
        temporary_record.write_text(
            json.dumps(_record_payload(record), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_record, generation_dir / "generation.json")
        return record
    except GenerationError:
        raise
    except Exception as error:
        raise GenerationError(f"Generation failed: {error}") from error


def _record_from_payload(payload: object) -> GenerationRecord:
    if not isinstance(payload, dict) or payload.get("schema_version") != GENERATION_RECORD_SCHEMA:
        raise ValueError("unsupported generation record schema")
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
                    or not _verify_outputs(record, root)):
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
    return posix.parts


def _verify_outputs(record: GenerationRecord, game_root: Path) -> bool:
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
    for relative, raw_path in record.output_files.items():
        parts = _safe_relative_output(relative)
        if parts is None:
            return False
        expected = generation_dir.joinpath(*parts)
        try:
            actual_path = Path(raw_path)
            if not actual_path.is_absolute():
                return False
            actual = actual_path.resolve(strict=True)
            expected = expected.resolve(strict=True)
            if actual != expected:
                return False
            try:
                actual.relative_to(generation_dir)
            except ValueError:
                return False
            if _hash_file(expected) != record.output_hashes[relative]:
                return False
        except OSError:
            return False
    return True


def compare_generation(record: GenerationRecord, game: GameInstallation,
                       source_overrides: dict[str, Path] | None = None) -> Freshness:
    """Compare staged inputs and classifier with a freshly scanned install."""
    if _normalized_game_root(record.game_root) != _normalized_game_root(game.root):
        return Freshness.STALE
    try:
        sources = _source_paths(game, record.primary_language,
                                 record.secondary_language, source_overrides)
        current_fingerprint = _fingerprint_sources(sources)
    except (OSError, PermissionError):
        return Freshness.UNREADABLE
    except (GenerationError, ValueError):
        return Freshness.STALE
    if current_fingerprint != record.source_fingerprint:
        return Freshness.STALE
    if not _verify_outputs(record, game.root):
        return Freshness.STALE
    if game.storefront is not record.storefront:
        return Freshness.STALE

    if record.mode is MergeMode.DIALOGUE_ONLY:
        try:
            index = _current_dialogue_index(game)
        except OSError:
            return Freshness.UNREADABLE
        if index is None or index.digest != record.classifier_digest:
            return Freshness.STALE
    elif record.classifier_digest is not None:
        return Freshness.STALE

    if game.version != record.game_version:
        return Freshness.VERSION_METADATA_CHANGED_ONLY
    return Freshness.CURRENT
