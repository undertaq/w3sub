"""Fail-closed classifier for localization IDs referenced by scene resources.

The verified Remastered 5.00 installation was inspected at
``F:\\Game\\SteamLibrary\\steamapps\\common\\The Witcher 3``. Under
``content\\content0`` it has language ``.w3strings`` files, 31 packed
``.bundle`` files, loose ``.ws`` scripts, and ``cookedfinal.redscripts``.
No standalone scene/quest reference files or confirmed reference schema were
found in the installed ``content`` tree. The local ``w3strings.exe`` only
decodes and encodes string tables; it does not expose bundle scene references.
Consequently this build cannot safely label dialogue contexts without another
verified source/parser, so live load/build deliberately return ``None``.

``DialogueIndex.from_validated_references`` is the boundary for a future parser
that has already confirmed a structured source schema. Tests use synthetic
references through that boundary; those fixtures do not establish live-game
dialogue-only support.
"""
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType

from .game import fingerprint_files
from .models import GameInstallation, GameVersion, ResourceFingerprint


INDEX_SCHEMA_VERSION = 1


class DialogContext(Enum):
    SCENE_SUBTITLE = "scene_subtitle"
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
    return str(int(string_id.strip())), key.strip().lower()


def _version_payload(version: GameVersion) -> dict[str, str | None]:
    return {
        "executable_version": version.executable_version,
        "executable_version_raw": version.executable_version_raw,
        "store_build_id": version.store_build_id,
    }


@dataclass(frozen=True, init=False)
class DialogueIndex:
    """Version/fingerprint-bound context map built from confirmed references."""

    game_version: GameVersion
    source_fingerprint: ResourceFingerprint
    source_scope: tuple[tuple[str, str], ...]
    schema_version: int
    digest: str
    _contexts: Mapping[Identity, frozenset[DialogContext]]

    def __init__(self, game_version: GameVersion,
                 source_fingerprint: ResourceFingerprint,
                 references: Mapping[tuple[str, str], Iterable[DialogContext]],
                 source_scope: Sequence[tuple[str, str]],
                 schema_version: int = INDEX_SCHEMA_VERSION):
        if schema_version != INDEX_SCHEMA_VERSION:
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
        object.__setattr__(self, "game_version", game_version)
        object.__setattr__(self, "source_fingerprint", source_fingerprint)
        object.__setattr__(self, "source_scope", normalized_scope)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "_contexts", frozen_contexts)
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
        """Check both the exact executable/store version and source bytes."""
        if not self.validated or game.version != self.game_version:
            return False
        try:
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
        contexts = self._contexts.get(identity, frozenset())
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


def load_dialogue_index(game: GameInstallation) -> DialogueIndex | None:
    """Load a compatible cached index when available.

    No supported cache format or validated context source is available for the
    current Remastered 5.00 installation, so this version returns ``None``.
    """
    return None


def build_dialogue_index(game: GameInstallation) -> DialogueIndex | None:
    """Build an index only when a confirmed structured reference parser exists.

    The normal installation's scene data remains in packed bundles, and the
    local toolchain has no verified schema/parser for those references. Do not
    infer usage context from ``.w3strings`` text; full-text merge remains usable.
    """
    return None


def dialogue_index_unavailable_reason(game: GameInstallation) -> str:
    """Explain why the current build cannot safely offer dialogue-only mode."""
    version = game.version.executable_version
    if version == "5.0.0.1044392":
        return (
            "Dialogue-only mode is unavailable for Remastered 5.00: the normal "
            "game install has no confirmed standalone scene-reference source, "
            "and this app has no verified parser for its packed bundle data. "
            "Full-text merge remains available."
        )
    return (
        f"No verified structured dialogue-reference parser is available for "
        f"game version {version}. Dialogue-only mode is disabled; full-text "
        "merge remains available."
    )
