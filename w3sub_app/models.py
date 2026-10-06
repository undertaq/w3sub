"""Shared discovery and game resource values."""
from dataclasses import dataclass, field
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
class AppConfig:
    last_game_root: Path | None = None
    converter_path: Path | None = None


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
    include_cutscenes: bool = True


@dataclass(frozen=True)
class ResourceFingerprint:
    entries: dict[str, str]
    digest: str


@dataclass(frozen=True)
class CutsceneGenerationSummary:
    # Sidecar resources are logical stems for the selected language pair,
    # plus malformed paths reported as skips. Changed counts exclude aliases.
    sidecar_discovered: int = 0
    sidecar_changed: int = 0
    sidecar_skipped: int = 0
    sidecar_unchanged: int = 0
    usm_discovered: int = 0
    usm_changed: int = 0
    usm_skipped: int = 0
    usm_unchanged: int = 0
    primary_cues: int = 0
    secondary_cues: int = 0
    matched_cues: int = 0
    # Counts unmatched cues, excluding resource-level skip diagnostics in CSV.
    unmatched_count: int = 0
    output_bytes: int = 0
    estimated_work_bytes: int = 0
    estimated_output_bytes: int = 0

    @property
    def match_ratio(self) -> float:
        return self.matched_cues / self.primary_cues if self.primary_cues else 0.0


@dataclass(frozen=True)
class GenerationRecord:
    generation_id: str
    game_root: Path
    generation_dir: Path
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
    codec_kind: str = "external"
    classifier_schema_version: int | None = None
    total_entries: int | None = None
    merged_entries: int | None = None
    unmatched_entries_count: int | None = None
    include_cutscenes: bool = False
    cutscene_output_files: dict[str, str] = field(default_factory=dict)
    cutscene_output_hashes: dict[str, str] = field(default_factory=dict)
    cutscene_summary: CutsceneGenerationSummary = field(default_factory=CutsceneGenerationSummary)
    cutscene_bundle_fingerprint: ResourceFingerprint | None = None
    cutscene_bundle_content_fingerprint: ResourceFingerprint | None = None


@dataclass(frozen=True)
class GenerationProvenance:
    codec_kind: str
    converter_path: str
    converter_sha256: str
    converter_version: str | None
    app_version: str
    classifier_schema_version: int | None


@dataclass(frozen=True)
class InstallTarget:
    relative_path: str
    backup_path: Path | None
    original_sha256: str | None
    installed_sha256: str
    original_exists: bool = True


@dataclass(frozen=True)
class InstallManifest:
    schema_version: int
    install_id: str
    game_root: Path
    state_directory: Path
    backup_directory: Path
    storefront: Storefront
    store_build_id: str | None
    generation_id: str
    generation_version: GameVersion
    install_version: GameVersion
    source_fingerprint: ResourceFingerprint
    primary_language: str
    secondary_language: str
    mode: MergeMode
    classifier_digest: str | None
    target_files: dict[str, InstallTarget]
    active: bool
    conflicted: bool = False
    conflict_paths: tuple[str, ...] = ()
    prepared: bool = False
    generation_provenance: GenerationProvenance | None = None
    cutscene_bundle_fingerprint: ResourceFingerprint | None = None
    cutscene_bundle_content_fingerprint: ResourceFingerprint | None = None
    created_directories: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallComparison:
    freshness: Freshness
    conflict_paths: tuple[str, ...] = ()

    @property
    def uninstall_safe(self) -> bool:
        return not self.conflict_paths


@dataclass(frozen=True)
class UninstallResult:
    restored_paths: tuple[str, ...]
    backup_directory: Path
    conflicts: tuple[str, ...] = ()
    rollback_errors: tuple[str, ...] = ()
    error: str | None = None
