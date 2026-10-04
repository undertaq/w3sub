"""Complete, version-bound localization context indexes; fail closed on gaps.

CR2W is the currently audited reference-bearing format, independent of its
filename extension. Every physical entry must be inspected, including entries
with no references. Unhandled payload formats cannot be proven reference-free
and leave the production index unavailable. The validated-source factory stays
available for parser integrations and isolated fixtures.
"""
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType

from .bundle_reader import enumerate_witcher_bundles, iter_bundle_entries, read_bundle_entry
from .config import state_root_for
from .game import fingerprint_files
from .models import GameInstallation, GameVersion, ResourceFingerprint
from .wolvenkit_scene_refs import (
    DEFAULT_HELPER_PATH, HelperIdentity, LocalizedReference, WolvenkitHelperSession,
    scan_localized_references, validate_wolvenkit_helper,
)


INDEX_SCHEMA_VERSION = 2
SUPPORTED_GAME_MAJOR_MINOR = (5, 0)
_CACHE_FILENAME = 'dialogue-index.json'
_STATUS_FILENAME = 'dialogue-index-status.json'
_AUDITED_FORMATS = ('CR2W',)
_BATCH_SIZE = 128


class DialogContext(Enum):
    SCENE_SUBTITLE = "scene_subtitle"
    CHOICE_UI = "choice_ui"
    OVERHEAD = "overhead"
    ITEM = "item"
    HUD_UI = "hud_ui"
    OBJECTIVE = "objective"
    OTHER = "other"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


Identity = tuple[str, str]


def _normalize_identity(string_id: str, key: str) -> Identity | None:
    if not isinstance(string_id, str) or not re.fullmatch(r"[0-9]+", string_id.strip()):
        return None
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]+", key.strip()):
        return None
    # Hash padding/case are spelling differences, not different game keys.
    # Ingestion and lookup share this function so aliases union their contexts.
    return str(int(string_id.strip())), format(int(key.strip(), 16), "x")


def _version_payload(version: GameVersion) -> dict[str, str | None]:
    return {
        "executable_version": version.executable_version,
        "executable_version_raw": version.executable_version_raw,
        "store_build_id": version.store_build_id,
    }


def _supported_major_minor(version: GameVersion) -> tuple[int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.", version.executable_version)
    if not match:
        return None
    major_minor = (int(match[1]), int(match[2]))
    return major_minor if major_minor == SUPPORTED_GAME_MAJOR_MINOR else None


@dataclass(frozen=True, init=False)
class DialogueIndex:
    """Version/fingerprint-bound context map built from confirmed references."""

    game_version: GameVersion
    source_fingerprint: ResourceFingerprint
    source_scope: tuple[tuple[str, str], ...]
    schema_version: int
    digest: str
    _contexts: Mapping[Identity, frozenset[DialogContext]]
    _id_contexts: Mapping[str, frozenset[DialogContext]]
    physical_inventory: tuple[tuple, ...]
    bundle_inventory: tuple[tuple[str, int], ...]
    helper_identity: HelperIdentity | None
    audited_formats: tuple[str, ...]

    def __init__(self, game_version: GameVersion,
                 source_fingerprint: ResourceFingerprint,
                 references: Mapping[tuple[str, str], Iterable[DialogContext]],
                 source_scope: Sequence[tuple[str, str]],
                 schema_version: int = INDEX_SCHEMA_VERSION, *,
                 id_references: Mapping[str, Iterable[DialogContext]] | None = None,
                 physical_inventory: Sequence[tuple] = (),
                 bundle_inventory: Sequence[tuple[str, int]] = (),
                 helper_identity: HelperIdentity | None = None,
                 audited_formats: Sequence[str] = ()):
        if type(schema_version) is not int or schema_version != INDEX_SCHEMA_VERSION:
            raise ValueError(f"Unsupported dialogue index schema: {schema_version}")
        if not source_fingerprint.entries:
            raise ValueError("A dialogue index requires at least one fingerprinted source")
        normalized_scope = tuple(sorted(set(source_scope)))
        if not normalized_scope:
            raise ValueError("A dialogue index requires a complete source inventory scope")
        for relative_root, pattern in normalized_scope:
            root_path = Path(relative_root)
            if (root_path.is_absolute() or ".." in root_path.parts
                    or not root_path.parts or not pattern or "/" in pattern
                    or "\\" in pattern or not _has_pattern_literal(pattern)):
                raise ValueError("Invalid source inventory root or filename pattern")

        contexts: dict[Identity, frozenset[DialogContext]] = {}
        for raw_identity, raw_contexts in references.items():
            if not isinstance(raw_identity, tuple) or len(raw_identity) != 2:
                raise ValueError(f"Invalid localization identity: {raw_identity!r}")
            identity = _normalize_identity(*raw_identity)
            if identity is None:
                raise ValueError(f"Invalid localization identity: {raw_identity!r}")
            normalized_contexts = frozenset(raw_contexts)
            if not all(isinstance(context, DialogContext) for context in normalized_contexts):
                raise ValueError(f"Invalid dialogue context for {raw_identity!r}")
            contexts[identity] = contexts.get(identity, frozenset()) | normalized_contexts

        frozen_contexts = MappingProxyType(dict(sorted(contexts.items())))
        id_contexts: dict[str, frozenset[DialogContext]] = {}
        for (string_id, _), values in contexts.items():
            id_contexts[string_id] = id_contexts.get(string_id, frozenset()) | values
        for raw_id, values in (id_references or {}).items():
            string_id = _normalize_string_id(raw_id)
            if string_id is None:
                raise ValueError(f'Invalid localization string ID: {raw_id!r}')
            values = frozenset(values)
            if not all(isinstance(value, DialogContext) for value in values):
                raise ValueError(f'Invalid dialogue context for {string_id}')
            id_contexts[string_id] = id_contexts.get(string_id, frozenset()) | values
        object.__setattr__(self, "game_version", game_version)
        object.__setattr__(self, "source_fingerprint", source_fingerprint)
        object.__setattr__(self, "source_scope", normalized_scope)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "_contexts", frozen_contexts)
        object.__setattr__(self, '_id_contexts', MappingProxyType(dict(sorted(id_contexts.items()))))
        object.__setattr__(self, 'physical_inventory', tuple(tuple(row) for row in physical_inventory))
        object.__setattr__(self, 'bundle_inventory', tuple(tuple(row) for row in bundle_inventory))
        object.__setattr__(self, 'helper_identity', helper_identity)
        object.__setattr__(self, 'audited_formats', tuple(audited_formats))
        if helper_identity is not None:
            _validate_bundle_metadata(self)
        object.__setattr__(self, "digest", self._calculate_digest())

    @classmethod
    def from_validated_references(
            cls, game: GameInstallation, source_paths: Sequence[Path],
            references: Mapping[tuple[str, str], Iterable[DialogContext]], *,
            source_roots: Sequence[Path],
            source_patterns: Sequence[str]) -> "DialogueIndex":
        """Create an index after a parser validates the reference schema.

        Callers describe the complete set of relevant files using narrow
        game-relative roots and filename-only glob patterns. The parsed input
        paths must equal that complete inventory at build time. References
        must come from structured inputs, never localized string wording.
        """
        scope = _normalize_source_scope(game, source_roots, source_patterns)
        discovered = _enumerate_source_inventory(game, scope)
        declared = _relative_source_paths(game, source_paths)
        if discovered != declared:
            raise ValueError(
                "Parsed source paths do not match the complete scoped source inventory"
            )
        fingerprint = fingerprint_files(game.root, discovered)
        return cls(game.version, fingerprint, references, scope)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "game_version": _version_payload(self.game_version),
            "source_fingerprint": {
                "entries": dict(sorted(self.source_fingerprint.entries.items())),
                "digest": self.source_fingerprint.digest,
            },
            "source_scope": [list(scope) for scope in self.source_scope],
            "contexts": [
                {
                    "string_id": identity[0],
                    "key": identity[1],
                    "contexts": sorted(context.value for context in values),
                }
                for identity, values in sorted(self._contexts.items())
            ],
            'id_contexts': {string_id: sorted(context.value for context in values)
                            for string_id, values in self._id_contexts.items()},
            'bundle_scan': {
                'physical_inventory': self.physical_inventory,
                'bundle_inventory': self.bundle_inventory,
                'helper_identity': asdict(self.helper_identity),
                'audited_formats': self.audited_formats,
            } if self.helper_identity is not None else None,
        }

    def _calculate_digest(self) -> str:
        encoded = json.dumps(self._payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def validated(self) -> bool:
        """Whether this index is internally sound; freshness is checked separately."""
        return (
            self.schema_version == INDEX_SCHEMA_VERSION
            and bool(self.source_fingerprint.entries)
            and bool(self.source_scope)
            and self.digest == self._calculate_digest()
        )

    def is_current(self, game: GameInstallation) -> bool:
        """Check supported major/minor compatibility and exact source bytes.

        Executable revision, raw version string, and storefront build metadata
        may change without invalidating the index, provided both game versions
        remain in the supported major/minor and its complete scoped reference
        inventory and hashes are unchanged.
        """
        if (not self.validated
                or _supported_major_minor(self.game_version) is None
                or _supported_major_minor(game.version) != _supported_major_minor(self.game_version)):
            return False
        try:
            if self.helper_identity is not None:
                bundles, entries, inventory, loose = _bundle_inventory(game)
                if (loose or bundles != self.bundle_inventory
                        or inventory != self.physical_inventory
                        or validate_wolvenkit_helper(DEFAULT_HELPER_PATH, check_protocol=False) != self.helper_identity):
                    return False
                hashes = {_entry_identity(game, entry): hashlib.sha256(
                    read_bundle_entry(entry.bundle_path, entry)).hexdigest() for entry in entries}
                return _payload_fingerprint(hashes) == self.source_fingerprint
            current_paths = _enumerate_source_inventory(game, self.source_scope)
            if current_paths != tuple(sorted(self.source_fingerprint.entries)):
                return False
            current = fingerprint_files(game.root, current_paths)
        except (OSError, ValueError, UnicodeError):
            return False
        return current == self.source_fingerprint

    def context_for(self, string_id: str, key: str) -> DialogContext:
        identity = _normalize_identity(string_id, key)
        if identity is None:
            return DialogContext.UNKNOWN
        return self.context_for_id(identity[0])

    def context_for_id(self, string_id: str) -> DialogContext:
        normalized = _normalize_string_id(string_id)
        if normalized is None:
            return DialogContext.UNKNOWN
        contexts = self._id_contexts.get(normalized, frozenset())
        if not contexts:
            return DialogContext.UNKNOWN
        if len(contexts) != 1:
            return DialogContext.AMBIGUOUS
        return next(iter(contexts))


def _normalize_source_scope(game: GameInstallation, roots: Sequence[Path],
                            patterns: Sequence[str]) -> tuple[tuple[str, str], ...]:
    game_root = Path(game.root).resolve()
    normalized_patterns = tuple(sorted(set(patterns)))
    if not roots or not normalized_patterns:
        raise ValueError("Source inventory requires at least one root and filename pattern")
    if any(not pattern or "/" in pattern or "\\" in pattern
           or not _has_pattern_literal(pattern) for pattern in normalized_patterns):
        raise ValueError("Source inventory patterns must match filenames only")

    scope = set()
    for root in roots:
        candidate = Path(root)
        absolute = (candidate if candidate.is_absolute() else game_root / candidate).resolve()
        relative = absolute.relative_to(game_root).as_posix()
        if relative == ".":
            raise ValueError("Source inventory roots must be narrower than the game root")
        if not absolute.is_dir():
            raise FileNotFoundError(f"Source inventory root is not a directory: {absolute}")
        for pattern in normalized_patterns:
            scope.add((relative, pattern))
    return tuple(sorted(scope))


def _has_pattern_literal(pattern: str) -> bool:
    return any(character not in "*?[]" for character in pattern)


def _relative_source_paths(game: GameInstallation, paths: Sequence[Path]) -> tuple[str, ...]:
    game_root = Path(game.root).resolve()
    relative = set()
    for path in paths:
        candidate = Path(path)
        absolute = (candidate if candidate.is_absolute() else game_root / candidate).resolve()
        relative.add(absolute.relative_to(game_root).as_posix())
    return tuple(sorted(relative))


def _enumerate_source_inventory(game: GameInstallation,
                                scope: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Enumerate only parser-declared roots/patterns, failing on scan errors."""
    game_root = Path(game.root).resolve()
    discovered = set()

    def raise_walk_error(error):
        raise error

    for relative_root, pattern in scope:
        source_root = (game_root / relative_root).resolve()
        source_root.relative_to(game_root)
        if not source_root.is_dir():
            raise FileNotFoundError(f"Source inventory root is not a directory: {source_root}")
        for parent, _, names in os.walk(source_root, onerror=raise_walk_error,
                                        followlinks=False):
            for name in names:
                if not fnmatch.fnmatchcase(name.casefold(), pattern.casefold()):
                    continue
                absolute = (Path(parent) / name).resolve()
                discovered.add(absolute.relative_to(game_root).as_posix())
    return tuple(sorted(discovered))


def _normalize_string_id(value: str) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value.strip()):
        return None
    number = int(value.strip())
    return str(number) if 0 <= number <= 0xFFFFFFFF else None


def _entry_identity(game: GameInstallation, entry) -> str:
    # JSON framing prevents path punctuation from aliasing distinct entries.
    relative = entry.bundle_path.resolve().relative_to(game.root.resolve()).as_posix()
    return json.dumps((relative, entry.entry_index, entry.depot_path), separators=(',', ':'))


def _payload_fingerprint(hashes: Mapping[str, str]) -> ResourceFingerprint:
    aggregate = hashlib.sha256()
    for identity, digest in sorted(hashes.items()):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid payload fingerprint')
        encoded = identity.encode('utf-8')
        aggregate.update(len(encoded).to_bytes(8, 'big'))
        aggregate.update(encoded)
        aggregate.update(bytes.fromhex(digest))
    return ResourceFingerprint(dict(sorted(hashes.items())), aggregate.hexdigest())


def _bundle_inventory(game: GameInstallation):
    """Inventory physical entries and detect uncovered loose files/linked roots.

    Loose files have no audited reference parser in this integration. Reporting
    them explicitly prevents a bundle-only scan from implying complete coverage
    of an installation that also carries external structured resources.
    """
    root = game.root.resolve()
    if not (root / 'content').is_dir():
        raise ValueError(f'{root / "content"}: missing active content directory')
    loose = []
    for name in ('content', 'dlc'):
        active = root / name
        try:
            active.stat()
        except FileNotFoundError:
            continue
        if not active.is_dir():
            raise ValueError(f'{active}: active resource root is not a directory')
        def walk_error(error):
            raise error
        for parent, directories, files in os.walk(active, onerror=walk_error, followlinks=False):
            directories[:] = sorted(d for d in directories if d.casefold() != 'dlc-tombstones')
            for candidate in [Path(parent), *(Path(parent) / d for d in directories),
                              *(Path(parent) / f for f in files)]:
                if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
                    raise ValueError(f'{candidate}: linked resource path prevents complete inventory')
                candidate.resolve().relative_to(root)
            loose.extend((Path(parent) / f).relative_to(root).as_posix()
                         for f in files if not f.casefold().endswith('.bundle'))
    paths = enumerate_witcher_bundles(root)
    if not paths:
        raise ValueError(f'{root / "content"}: no active packed bundle data found')
    bundles = tuple((path.resolve().relative_to(root).as_posix(), path.stat().st_size) for path in paths)
    entries = tuple(entry for path in paths for entry in iter_bundle_entries(path))
    if not entries:
        raise ValueError(f'{root / "content"}: empty physical resource inventory')
    physical = tuple((entry.bundle_path.resolve().relative_to(root).as_posix(),
                      entry.entry_index, entry.depot_path, entry.offset,
                      entry.compressed_size, entry.uncompressed_size,
                      entry.crc32, entry.compression_method) for entry in entries)
    return bundles, entries, physical, tuple(sorted(loose))


def _validate_bundle_metadata(index: DialogueIndex) -> None:
    if (not index.physical_inventory or not index.bundle_inventory
            or index.audited_formats != _AUDITED_FORMATS):
        raise ValueError('Incomplete or unsupported bundle scan coverage')
    identity = index.helper_identity
    if (not isinstance(identity, HelperIdentity) or identity.supports_v164 is not True
            or not all(isinstance(value, str) and value for value in
                       (identity.version, identity.upstream_commit, identity.executable_sha256))
            or not re.fullmatch('[0-9a-f]{64}', identity.executable_sha256)):
        raise ValueError('Invalid helper identity')
    bundle_paths = set()
    for row in index.bundle_inventory:
        if (len(row) != 2 or not isinstance(row[0], str)
                or type(row[1]) is not int or row[1] < 32
                or Path(row[0]).is_absolute() or '..' in Path(row[0]).parts
                or not Path(row[0]).parts or Path(row[0]).parts[0] not in ('content', 'dlc')
                or row[0] in bundle_paths):
            raise ValueError('Malformed bundle inventory')
        bundle_paths.add(row[0])
    if index.source_scope != tuple((name, '*.bundle') for name in
                                   sorted({Path(path).parts[0] for path in bundle_paths})):
        raise ValueError('Incomplete bundle inventory scope')
    identities = set()
    for row in index.physical_inventory:
        if (len(row) != 8 or row[0] not in bundle_paths or not isinstance(row[2], str)
                or not row[2] or any(type(row[i]) is not int or row[i] < 0 for i in (1, 3, 4, 5, 6, 7))
                or row[6] > 0xFFFFFFFF or row[7] not in (0, 1)):
            raise ValueError('Malformed physical entry inventory')
        identity_key = json.dumps(row[:3], separators=(',', ':'))
        if identity_key in identities:
            raise ValueError('Duplicate physical entry identity')
        identities.add(identity_key)
    if identities != set(index.source_fingerprint.entries):
        raise ValueError('Payload hashes do not cover every physical entry')
    if _payload_fingerprint(index.source_fingerprint.entries) != index.source_fingerprint:
        raise ValueError('Invalid aggregate source fingerprint')


def _state_directory(game: GameInstallation) -> Path:
    state = state_root_for(game.root).resolve()
    if state.is_relative_to(game.root.resolve()):
        raise ValueError('Dialogue cache must be outside the game installation')
    return state


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.dialogue-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _record_failure(game: GameInstallation, error: Exception, counts=None, *, remove_cache=False) -> None:
    try:
        state = _state_directory(game)
        if remove_cache:
            (state / _CACHE_FILENAME).unlink(missing_ok=True)
        _atomic_json(state / _STATUS_FILENAME, {
            'state': 'unavailable' if remove_cache else 'stale',
            'game_version': _version_payload(game.version),
            'diagnostic': str(error), 'counts': counts or {},
        })
    except (OSError, ValueError):
        # A read-only/unavailable app cache also cannot enable Dialogue-only.
        pass


def load_dialogue_index(game: GameInstallation) -> DialogueIndex | None:
    """Validate a complete schema-2 cache without launching a reference scan."""
    try:
        if _supported_major_minor(game.version) is None:
            raise ValueError(f'Unsupported game version {game.version.executable_version}; 5.0 required')
        path = _state_directory(game) / _CACHE_FILENAME
        if not path.exists():
            return None
        document = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
                or document['schema_version'] != INDEX_SCHEMA_VERSION
                or not isinstance(document.get('bundle_scan'), dict)):
            raise ValueError(f'{path}: incomplete or unsupported cache schema')
        scan = document['bundle_scan']
        fingerprint = document['source_fingerprint']
        index = DialogueIndex(
            GameVersion(**document['game_version']),
            ResourceFingerprint(fingerprint['entries'], fingerprint['digest']), {},
            tuple(tuple(scope) for scope in document['source_scope']), document['schema_version'],
            id_references={string_id: [DialogContext(value) for value in contexts]
                           for string_id, contexts in document['id_contexts'].items()},
            physical_inventory=scan['physical_inventory'], bundle_inventory=scan['bundle_inventory'],
            helper_identity=HelperIdentity(**scan['helper_identity']), audited_formats=scan['audited_formats'],
        )
        if (set(document) != set(index._payload()) | {'digest'}
                or document['contexts'] != [] or document['digest'] != index.digest):
            raise ValueError(f'{path}: cache digest or structure mismatch')
        if not index.is_current(game):
            raise ValueError(f'{path}: cached game inventory, payloads, version, or helper changed; rebuild required')
        (_state_directory(game) / _STATUS_FILENAME).unlink(missing_ok=True)
        return index
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError) as error:
        _record_failure(game, error)
        return None


def _reference_context(reference: LocalizedReference, expected: set[str]) -> tuple[str, DialogContext]:
    if (not isinstance(reference, LocalizedReference) or type(reference.string_id) is not int
            or not 0 <= reference.string_id <= 0xFFFFFFFF or reference.resource_identity not in expected
            or not isinstance(reference.owner_type, str) or not reference.owner_type.strip()
            or not isinstance(reference.field_name, str) or not reference.field_name.strip()):
        raise ValueError(f'Malformed typed reference: {getattr(reference, "resource_identity", "unknown resource")}')
    context = {('CStorySceneLine', 'dialogLine'): DialogContext.SCENE_SUBTITLE,
               ('CStorySceneChoiceLine', 'choiceLine'): DialogContext.CHOICE_UI}.get(
                   (reference.owner_type, reference.field_name), DialogContext.OTHER)
    return str(reference.string_id), context


def build_dialogue_index(
        game: GameInstallation,
        progress_callback: Callable[[int, int], None] | None = None) -> DialogueIndex | None:
    """Build only from fully inventoried, verified and parsed active resources.

    Persistent JSON contains IDs, contexts, hashes and identities only. Extracted
    resources live in a temporary app-state directory removed even on failure.
    An unaudited format is an explicit coverage failure, never an ignored file.
    """
    counts = {'bundles': 0, 'physical_entries': 0, 'verified_resources': 0, 'parsed_resources': 0}
    try:
        if _supported_major_minor(game.version) is None:
            raise ValueError(f'Unsupported game version {game.version.executable_version}; 5.0 required')
        state = _state_directory(game)
        bundles, entries, physical, loose = _bundle_inventory(game)
        counts.update(bundles=len(bundles), physical_entries=len(entries), loose_files=len(loose))
        formats = {}
        for entry in entries:
            suffix = Path(entry.depot_path.replace('\\', '/')).suffix.casefold() or '<no extension>'
            formats[suffix] = formats.get(suffix, 0) + 1
        counts['inventory_formats'] = formats
        loose_formats = {}
        for path in loose:
            suffix = Path(path).suffix.casefold() or '<no extension>'
            loose_formats[suffix] = loose_formats.get(suffix, 0) + 1
        counts['loose_formats'] = loose_formats
        counts['audited_formats'] = list(_AUDITED_FORMATS)
        if loose:
            raise ValueError(f'{loose[0]}: unaudited loose resource format; bundle inventory alone is incomplete')
        session = WolvenkitHelperSession(DEFAULT_HELPER_PATH)
        helper = session.identity
        hashes = {}
        contexts = {}
        state.mkdir(parents=True, exist_ok=True)
        if progress_callback:
            progress_callback(0, len(entries))
        with tempfile.TemporaryDirectory(prefix='.dialogue-scan-', dir=state) as temporary:
            workspace = Path(temporary)
            for start in range(0, len(entries), _BATCH_SIZE):
                resources = []
                for position, entry in enumerate(entries[start:start + _BATCH_SIZE], start):
                    identity = _entry_identity(game, entry)
                    try:
                        payload = read_bundle_entry(entry.bundle_path, entry)
                    except ValueError as error:
                        raise ValueError(f'{identity}: {error}') from error
                    counts['verified_resources'] += 1
                    if not payload.startswith(b'CR2W'):
                        raise ValueError(f'{identity}: unaudited non-CR2W payload format; reference coverage unknown')
                    hashes[identity] = hashlib.sha256(payload).hexdigest()
                    extracted = workspace / f'{position}.cr2w'
                    extracted.write_bytes(payload)
                    resources.append({'path': str(extracted.resolve()), 'resource_identity': identity})
                manifest = workspace / 'manifest.json'
                _atomic_json(manifest, {'schema': 1, 'resources': resources})
                expected = {resource['resource_identity'] for resource in resources}
                references = scan_localized_references(
                    manifest, DEFAULT_HELPER_PATH, workspace,
                    (lambda done, total: progress_callback(start + done, len(entries))) if progress_callback else None,
                    session=session,
                )
                for reference in references:
                    string_id, context = _reference_context(reference, expected)
                    contexts.setdefault(string_id, set()).add(context)
                counts['parsed_resources'] += len(resources)
                for resource in resources:
                    Path(resource['path']).unlink()
        scope = tuple((name, '*.bundle') for name in sorted({Path(row[0]).parts[0] for row in bundles}))
        index = DialogueIndex(game.version, _payload_fingerprint(hashes), {}, scope,
                              id_references=contexts, physical_inventory=physical,
                              bundle_inventory=bundles, helper_identity=helper,
                              audited_formats=_AUDITED_FORMATS)
        # Guard against an upgrade or replacement while the worker was parsing.
        if not index.is_current(game):
            raise ValueError('Game resource inventory, payloads or helper changed during index construction')
        _atomic_json(state / _CACHE_FILENAME, {**index._payload(), 'digest': index.digest})
        (state / _STATUS_FILENAME).unlink(missing_ok=True)
        if progress_callback:
            progress_callback(len(entries), len(entries))
        return index
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        _record_failure(game, error, counts, remove_cache=True)
        return None


def dialogue_index_unavailable_reason(game: GameInstallation) -> str:
    """Expose the last path-specific failure without repeating a full scan."""
    reason = f'No complete dialogue index has been built for game version {game.version.executable_version}'
    if _supported_major_minor(game.version) is None:
        reason = f'Unsupported game version {game.version.executable_version}; 5.0 required'
    else:
        try:
            status = json.loads((_state_directory(game) / _STATUS_FILENAME).read_text(encoding='utf-8'))
            if isinstance(status, dict) and isinstance(status.get('diagnostic'), str):
                reason = status['diagnostic']
        except (OSError, ValueError, UnicodeError):
            pass
    return f'Dialogue-only mode is unavailable: {reason}. Full-text merge remains available.'
