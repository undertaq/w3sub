"""Shared discovery and game resource values."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Storefront(Enum):
    STEAM = "steam"
    GOG = "gog"
    EPIC = "epic"
    UNKNOWN = "unknown"


class MergeMode(Enum):
    FULL_TEXT = "full_text"
    DIALOGUE_ONLY = "dialogue_only"


class Freshness(Enum):
    CURRENT = "current"
    VERSION_METADATA_CHANGED_ONLY = "version_metadata_changed_only"
    STALE = "stale"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class GameCandidate:
    root: Path
    storefront: Storefront
    record_source: str
    store_build_id: str | None = None


@dataclass(frozen=True)
class GameVersion:
    executable_version: str
    store_build_id: str | None
    executable_version_raw: str | None = None


@dataclass(frozen=True)
class GameInstallation:
    root: Path
    storefront: Storefront
    version: GameVersion
    language_files: dict[str, tuple[Path, ...]]


@dataclass(frozen=True)
class GenerationRequest:
    game: GameInstallation
    primary_language: str
    secondary_language: str
    mode: MergeMode
    source_overrides: dict[str, Path] | None = None


@dataclass(frozen=True)
class ResourceFingerprint:
    entries: dict[str, str]
    digest: str


@dataclass(frozen=True)
class GenerationRecord:
    generation_id: str
    game_root: Path
    storefront: Storefront
    game_version: GameVersion
    primary_language: str
    secondary_language: str
    mode: MergeMode
    source_fingerprint: ResourceFingerprint
    classifier_digest: str | None
    converter_path: str
    converter_sha256: str
    converter_version: str | None
    app_version: str
    output_files: dict[str, str]
    output_hashes: dict[str, str]
