"""Shared storefront discovery values."""
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
