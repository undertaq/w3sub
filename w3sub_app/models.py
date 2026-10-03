"""Shared discovery and game resource values."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Storefront(Enum):
    STEAM = "steam"
    GOG = "gog"
    EPIC = "epic"
    UNKNOWN = "unknown"


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
class ResourceFingerprint:
    entries: dict[str, str]
    digest: str
