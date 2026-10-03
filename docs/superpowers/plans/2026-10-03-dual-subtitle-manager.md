# Witcher 3 Dual Subtitle Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows GUI that creates, installs, modifies, and uninstalls a version-aware dual-language subtitle update for The Witcher 3: Wild Hunt — Remastered 5.00.

**Architecture:** Keep the existing Python project and `w3strings.exe`, split the current script into focused game discovery, conversion, merge, generation, install, and GUI modules. Store backups and generation metadata under the user's local application data, discover Steam/GOG/Epic installs from registry and storefront records, and only enable dialogue-only mode when a build-matched context index can be verified.

**Tech Stack:** Python 3.10+, standard-library Tkinter, `subprocess`, `winreg`, JSON, SHA-256, and the existing `w3strings.exe` v0.4.1. Use Python's `unittest` for isolated module checks and copied game fixtures; no third-party runtime dependency is introduced.

**Spec:** `docs/superpowers/specs/2026-10-03-witcher3-dual-subtitle-manager-design.md`

## Global Constraints

- Target Windows and The Witcher 3: Wild Hunt — Remastered 5.00.
- Accept the game executable under `bin\x64` or `bin\x64_dx12`; reject unsupported major/minor versions.
- Discover Steam, GOG, and Epic installations; validate candidates and allow manual browsing. Require `content` and a supported executable; scan `dlc` only when present and never treat `dlc-tombstones` as active resources.
- Use standard-library Tkinter and the bundled `w3strings.exe`; do not add a GUI runtime dependency.
- Store staging data, generation records, and backups outside the game directory under local application data, keyed by normalized game path.
- Never use the game directory as scratch space or recursively delete game paths.
- Pair resources and records by relative path, string ID, and key; keep primary text first and retain primary-only records.
- Dialogue-only mode may combine only confirmed in-dialogue scene subtitle IDs from a version-matched context index; unknown or ambiguous IDs stay primary-only.
- Compare game version and source resource fingerprints before install/modify; stale generation output cannot be installed.
- Preserve third-party changes on hash conflict and roll back partial installs from exact saved originals.
- Do not touch the repository's existing untracked `backup/`, `working/`, `install/`, `images/`, or ZIP files.

## Review Focus

- Stale or duplicate storefront records, including Epic installs represented by manifests rather than game-specific registry paths — Task 1 must assert candidate validation and canonical-path deduplication.
- Remastered executable layouts and game upgrades that change language assets while version metadata stays the same, or change only version metadata — Tasks 2 and 5 must assert version detection and freshness decisions.
- Dialogue IDs used by multiple resource contexts, overhead/oneliner IDs, and unknown IDs — Task 4 must assert only confirmed in-scene subtitle IDs are eligible.
- CSV text containing delimiters and `<br>` markers, missing or duplicate records, malformed input, and converter failures — Task 3 must assert safe parsing and fail-closed conversion.
- Third-party edits, locked files, and failures after only part of a replacement set was installed — Task 6 must assert conflict detection and exact rollback behavior.

---

## File Structure

Create a small package beside the existing launcher:

- `w3sub_app/models.py` — shared enums and immutable dataclasses passed between modules.
- `w3sub_app/storefronts.py` — registry and Steam/GOG/Epic local-record discovery.
- `w3sub_app/game.py` — game-root validation, executable/version reading, language inventory, and input fingerprints.
- `w3sub_app/converter.py` — safe adapter around `w3strings.exe`.
- `w3sub_app/merge.py` — CSV parsing and the full-text/dialogue-only merge policy.
- `w3sub_app/dialogue_index.py` — version-bound dialogue context index loading/building and validation.
- `w3sub_app/generation.py` — staging, source revalidation, generation records, and output inventory.
- `w3sub_app/install.py` — manifest, backup, transactional install/modify/uninstall, and conflict checks.
- `w3sub_app/config.py` — saved game choice and application-state paths.
- `w3sub_app/gui.py` — Tkinter interface and background operation coordination.
- `w3sub_app/__init__.py` — package marker and application version.
- `w3sub.py` — stable launcher that opens the GUI and never mutates the game on launch.
- `tests/` — standard-library unit tests for parsing, discovery, merge, freshness, and transactional file operations.
- `tests/__init__.py` — make focused tests importable through `python -m unittest`.
- `README.md` — startup, supported stores/builds, lifecycle, and dialogue-only limitations.

Use the following shared interfaces; later tasks consume these names without changing them:

```python
class Storefront(Enum):
    STEAM = "steam"
    GOG = "gog"
    EPIC = "epic"
    UNKNOWN = "unknown"

class MergeMode(Enum):
    FULL_TEXT = "full_text"
    DIALOGUE_ONLY = "dialogue_only"

@dataclass(frozen=True)
class GameVersion:
    executable_version: str
    store_build_id: str | None
    executable_version_raw: str | None = None  # exact Windows file/product version resource string

@dataclass(frozen=True)
class GameCandidate:
    root: Path
    storefront: Storefront
    record_source: str
    store_build_id: str | None = None

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
    source_overrides: dict[str, Path] | None = None  # game-relative resource -> original baseline file; used by Modify

@dataclass(frozen=True)
class AppConfig:
    last_game_root: Path | None

@dataclass(frozen=True)
class ResourceFingerprint:
    entries: dict[str, str]  # relative path -> SHA-256
    digest: str

@dataclass(frozen=True)
class GenerationRecord:
    generation_id: str
    game_root: Path
    game_version: GameVersion
    primary_language: str
    secondary_language: str
    mode: MergeMode
    source_fingerprint: ResourceFingerprint
    classifier_digest: str | None
    output_files: dict[str, str]  # game-relative path -> staged absolute path
```

---

### Task 1: Storefront install discovery

**Files:**
- Create: `w3sub_app/models.py`
- Create: `w3sub_app/storefronts.py`
- Create: `tests/test_storefronts.py`
- Create: `tests/__init__.py`

**Interfaces:**
- Produces `Storefront`, `GameCandidate`, and `discover_candidates() -> list[GameCandidate]` for Tasks 2 and 7.
- `discover_candidates(registry: RegistryReader | None = None, roots: StoreRoots | None = None) -> list[GameCandidate]` accepts injected readers/roots for tests; production reads Windows registry without elevating privileges.
- Define `RegistryReader` and `StoreRoots` in `storefronts.py`; use fixtures in tests.

- [ ] **Step 1: Add discovery tests** for Steam registry path plus `libraryfolders.vdf`/App ID 292030 manifest, GOG game/uninstall registry entries in both registry views, and Epic registry overrides plus JSON install manifests under the normal and launcher-relative manifest roots. Assert stale records are rejected and matching canonical roots deduplicate with all provenance retained in `record_source` as distinct source labels joined by `; `; preserve an optional storefront `store_build_id` when a local record provides one.
- [ ] **Step 2: Run the focused tests and confirm they fail** before implementing discovery.

  Run: `python -m unittest tests.test_storefronts -v`

- [ ] **Step 3: Implement registry and record readers** using `winreg` on Windows. Search HKCU and HKLM 32-bit/64-bit views; treat all discovered paths as candidates, not trusted roots. Parse Steam VDF records (including `buildid`) and Epic manifest JSON (including `AppVersion`) without launching the storefronts. Search GOG records by game name and known game registry locations, not by a fixed install directory, and retain a version/build value when present.
- [ ] **Step 4: Validate, normalize, and deduplicate candidates.** Require a plausible Witcher 3 root with `content` and a supported executable before returning it. Treat `dlc` as optional, and never treat `dlc-tombstones` as active resources. Preserve a human-readable `record_source` for the GUI. On non-Windows systems or malformed records, return no candidate and leave manual browsing available.
- [ ] **Step 5: Run the focused tests** and manually inspect the local result; it should find the F: Steam library and should not claim the Epic EOS helper registry paths are game installs.

  Run: `python -m unittest tests.test_storefronts -v`

- [ ] **Step 6: Commit** `feat: discover Witcher 3 installs across storefronts`.

### Task 2: Game validation, version, languages, and fingerprints

**Files:**
- Modify: `w3sub_app/models.py`
- Create: `w3sub_app/game.py`
- Create: `tests/test_game.py`

**Interfaces:**
- Consumes `GameCandidate` from Task 1.
- Produces `scan_game(root: Path, storefront: Storefront, store_build_id: str | None = None, version_reader: VersionReader | None = None) -> GameInstallation`, `find_game_executable(root: Path) -> Path`, and `fingerprint_files(root: Path, paths: Sequence[Path]) -> ResourceFingerprint`.

- [ ] **Step 1: Add tests** for `bin\x64` and `bin\x64_dx12`, accepting a 5.0 executable, rejecting Classic 1.32 and 4.x, extracting all language codes from present `.w3strings` assets, retaining an optional passed `store_build_id`, preserving the raw Windows version resource beside its normalized numeric version, and hashing a path inventory deterministically. Inject `version_reader` so version rules do not depend on the host game install.
- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run: `python -m unittest tests.test_game -v`

- [ ] **Step 3: Implement executable version reading** with the Windows version-information API via `ctypes`; keep the exact file/product version string in `GameVersion.executable_version_raw` and use its anchored leading numeric version as `executable_version` for validation and display. Carry the optional `store_build_id` passed from `GameCandidate` into `GameVersion`; Task 1 storefront readers own parsing Steam `buildid`, GOG version/build values, and Epic `AppVersion` when available.
- [ ] **Step 4: Implement root scanning.** Require `content` and Remastered 5.0 major/minor; prefer the `bin\x64_dx12\witcher3.exe` target when both supported executables exist, then `bin\x64\witcher3.exe`. Inventory `.w3strings` basenames under `content` and under `dlc` when present; do not scan `dlc-tombstones`. Return language-to-path tuples sorted by relative path.
- [ ] **Step 5: Implement deterministic fingerprints** over the sorted relative paths and file bytes, recording per-file SHA-256 plus an aggregate digest. Fail if any input cannot be read; never report a partial fingerprint as valid.
- [ ] **Step 6: Run the focused tests**, then scan the known F: installation and confirm it reports version `5.0.0.1044392` and the locally observed language set.

  Run: `python -m unittest tests.test_game -v`

- [ ] **Step 7: Commit** `feat: scan Remastered game versions and language assets`.

### Task 3: Converter adapter and full-text merge

**Files:**
- Create: `w3sub_app/converter.py`
- Create: `w3sub_app/merge.py`
- Create: `tests/test_converter.py`
- Create: `tests/test_merge.py`

**Interfaces:**
- Produces `W3StringsConverter(executable: Path)`, `decode(source: Path, work_dir: Path) -> Path`, `encode(csv_path: Path, work_dir: Path) -> Path`, and `merge_csv(primary: Path, secondary: Path, mode: MergeMode, dialogue_index: DialogueIndex | None) -> Path`.
- `DialogueIndex` is defined in Task 4; Task 3 must accept `None` and raise `MergeError` for dialogue-only mode if no validated index is supplied.

- [ ] **Step 1: Add tests** for converter exit errors, argument-safe paths containing spaces, CSV metadata comments, text with `|` and `<br>`, record matching by `(id, key)`, primary-only retention, secondary-only omission, and primary-first `<br>` output in full-text mode.
- [ ] **Step 2: Run focused tests and confirm they fail.**

  Run: `python -m unittest tests.test_converter tests.test_merge -v`

- [ ] **Step 3: Implement the converter adapter** using `subprocess.run([...], cwd=work_dir, capture_output=True, text=True, check=False)` with no shell. Decode with `--decode`; encode with `--encode` and the existing `--force-ignore-id-space-check-i-know-what-i-am-doing` option. Copy source assets into managed staging before conversion. Require exit code zero and expected output file existence; include captured diagnostics in `ConverterError`.
- [ ] **Step 4: Implement the CSV parser** using the converter's four-column pipe format. Split each data row at most three times so delimiters inside text stay in the text field. Preserve comments/header metadata and source ordering; reject malformed rows and ambiguous duplicate keys.
- [ ] **Step 5: Implement full-text merge** by matching relative resources and `(string_id, key)`, retaining primary-only records, ignoring secondary-only records, and joining matching text as `primary + "<br>" + secondary`.
- [ ] **Step 6: Validate converter compatibility** on copies of representative 5.00 source files using decode/re-encode checks supported by v0.4.1; keep the live game and the repository's generated trees unchanged.

  Run: `python -m unittest tests.test_converter tests.test_merge -v`

- [ ] **Step 7: Commit** `feat: convert and merge localized string files`.

### Task 4: Version-matched dialogue context index

**Files:**
- Create: `w3sub_app/dialogue_index.py`
- Modify: `w3sub_app/merge.py`
- Create: `tests/test_dialogue_index.py`
- Modify: `tests/test_merge.py`

**Interfaces:**
- Produces `DialogContext`, `DialogueIndex`, `load_dialogue_index(game: GameInstallation) -> DialogueIndex | None`, and `build_dialogue_index(game: GameInstallation) -> DialogueIndex | None`.
- `DialogueIndex.context_for(string_id: str, key: str) -> DialogContext` returns one of `SCENE_SUBTITLE`, `OVERHEAD`, `ITEM`, `HUD_UI`, `OBJECTIVE`, `OTHER`, `AMBIGUOUS`, or `UNKNOWN`; the index also exposes `game_version: GameVersion`, `source_fingerprint: ResourceFingerprint`, and `digest: str`.
- Task 3 consumes the index and merges only `SCENE_SUBTITLE` rows in `DIALOGUE_ONLY` mode.

- [ ] **Step 1: Probe the local Remastered 5.00 resource structure** and identify a structured source that links localization IDs to in-dialogue scene subtitles and distinguishes them from gameplay oneliners/overhead text, items, and UI. Record the exact source paths/schema and whether they can be parsed from a normal game installation without REDkit being installed.
- [ ] **Step 2: Add classifier tests** for a known scene subtitle, overhead/oneliner, item, objective, multiply-used ID, unknown ID, and stale index fingerprint. Unknown, ambiguous, and every non-`SCENE_SUBTITLE` context must remain primary-only.
- [ ] **Step 3: Implement a version-keyed index only from confirmed structured references.** Store the executable version, relevant source-resource fingerprints, index schema version, and index digest. Never infer context from text length, punctuation, or wording.
- [ ] **Step 4: Integrate dialogue-only merge.** If the index is missing, stale, or cannot classify an ID, leave that primary row unchanged. If no supported context source can be parsed for a build, return `None` so the GUI can disable dialogue-only mode while full-text mode remains available.
- [ ] **Step 5: Run focused checks** and inspect representative output rows from the selected build; verify that only known in-scene subtitle IDs receive secondary text and overhead, item, UI, objective, ambiguous, and unknown entries do not.

  Run: `python -m unittest tests.test_dialogue_index tests.test_merge -v`

- [ ] **Step 6: Commit** `feat: classify scene subtitle strings by resource context`.

### Task 5: Generation records and stale-output detection

**Files:**
- Create: `w3sub_app/generation.py`
- Create: `tests/test_generation.py`
- Modify: `w3sub_app/models.py`

**Interfaces:**
- Produces `generate(request: GenerationRequest, state_root: Path, converter: W3StringsConverter) -> GenerationRecord`, `compare_generation(record: GenerationRecord, game: GameInstallation, source_overrides: dict[str, Path] | None = None) -> Freshness`, and `Freshness` values `CURRENT`, `VERSION_METADATA_CHANGED_ONLY`, `STALE`, and `UNREADABLE`.
- Staging and generation records live outside the game tree under `%LOCALAPPDATA%\W3DualSubtitle\games\<normalized-root-hash>\generations\<generation_id>`.

- [ ] **Step 1: Add tests** for version-only changes with identical fingerprints, changed/added/removed source files, unreadable files, classifier-index changes, and a version/source update between generation and install.
- [ ] **Step 2: Implement staging and generation metadata.** Generate all outputs into a new generation directory; persist storefront, game version at generation, language pair, mode, source file hashes, classifier digest when used, converter/app versions, and output hashes in JSON. Resolve each override by game-relative resource path and fingerprint the override bytes under that same logical path.
- [ ] **Step 3: Implement freshness comparison.** Treat changed or unreadable source inventories as stale. If only executable/store build metadata changed and language fingerprints plus classifier digest are identical, report metadata-only change and allow use. Accept optional source overrides so Modify can compare against the exact saved originals in the active manifest. Recheck immediately before installation.
- [ ] **Step 4: Run focused checks** and inspect a generated record without writing anything into the live game folder.

  Run: `python -m unittest tests.test_generation -v`

- [ ] **Step 5: Commit** `feat: track generation inputs and game versions`.

### Task 6: Transactional install, modify, and uninstall

**Files:**
- Create: `w3sub_app/install.py`
- Create: `tests/test_install.py`
- Modify: `w3sub_app/models.py`

**Interfaces:**
- Produces `install_generation(game: GameInstallation, generation: GenerationRecord, state_root: Path) -> InstallManifest`, `modify_install(game: GameInstallation, generation: GenerationRecord, manifest: InstallManifest) -> InstallManifest`, and `uninstall(game: GameInstallation, manifest: InstallManifest) -> UninstallResult`.
- Also produces `load_install_manifest(state_root: Path) -> InstallManifest | None` and `compare_install(manifest: InstallManifest, game: GameInstallation) -> Freshness` for startup/folder-change upgrade detection.
- Manifest stores storefront/build identity, generation and install versions, input fingerprints, pair/mode, classifier digest, every target path, original backup path/hash, installed hash, and active/conflicted state.
- `InstallManifest` is JSON-serializable; `UninstallResult` reports restored paths, backup directory, and any conflict/rollback errors.
- State and exact backups live at `%LOCALAPPDATA%\W3DualSubtitle\games\<normalized-root-hash>`.

- [ ] **Step 1: Add tests** in temporary copied game trees for a successful install/modify/uninstall cycle, install failure after the first replacement, rollback failure reporting, external file edits, game update replacement, locked-file errors, and preservation of unexpected files.
- [ ] **Step 2: Implement preflight and an exclusive per-game operation lock.** Require a current generation record, writable target/state paths, closed game files, and no hash conflict. Acquire a lock atomically before any lifecycle mutation and release it in `finally`; report a stale lock for explicit recovery. List exact target files in the operation result. Use `compare_install` to mark an active install stale when source fingerprints change and conflicted when installed hashes no longer match.
- [ ] **Step 3: Implement install transaction.** Back up and hash all originals first, write a prepared manifest, stage replacements beside targets, replace using `os.replace`, verify installed hashes, then mark active. On failure, restore already replaced targets and report every rollback result.
- [ ] **Step 4: Implement modify from originals.** Require every installed hash to match. Generate from the backed-up baseline rather than merged output using Task 5's `source_overrides`; require its source fingerprint to match the manifest's original backup hashes, then update the manifest only after a verified transaction.
- [ ] **Step 5: Implement uninstall and update conflict handling.** Restore only when all managed installed hashes match. Preserve backup sets after uninstall. On mismatch, do not overwrite the file; mark conflicted and expose affected paths plus backup location.
- [ ] **Step 6: Run focused tests** exclusively against temporary copied trees; verify byte-identical originals after uninstall and after successful rollback.

  Run: `python -m unittest tests.test_install -v`

- [ ] **Step 7: Commit** `feat: add safe install modify and uninstall lifecycle`.

### Task 7: GUI, configuration, and launcher

**Files:**
- Create: `w3sub_app/config.py`
- Create: `w3sub_app/gui.py`
- Create: `w3sub_app/__init__.py`
- Modify: `w3sub.py`
- Modify: `w3sub_app/models.py`
- Create: `tests/test_config.py`

**Interfaces:**
- GUI calls `discover_candidates`, `scan_game`, `generate`, `compare_generation`, `load_install_manifest`, `compare_install`, `install_generation`, `modify_install`, and `uninstall` from earlier tasks.
- Config exposes `load_config() -> AppConfig`, `save_config(config: AppConfig) -> None`, and `state_root_for(game_root: Path) -> Path`.

- [ ] **Step 1: Add config tests** for missing/invalid JSON, last valid game path fallback, stable normalized path hashes, and isolated per-game state directories.
- [ ] **Step 2: Implement config/state paths** under `%LOCALAPPDATA%\W3DualSubtitle`; save last selection without storing credentials or storefront account data. Configure standard-library logging to an application log in this state root, retaining converter and rollback diagnostics while showing concise errors in the GUI.
- [ ] **Step 3: Implement the Tkinter screen** with discovered-install selection, manual folder browse, detected version/store/languages, primary/secondary dropdowns, full-text/dialogue-only choice, scan status, generation preview, progress, and active Install/Modify/Uninstall actions. On startup, use registry/store discovery before the saved path; after any folder change, rescan and compare game/generation/install metadata. For Modify, build `GenerationRequest.source_overrides` from the manifest's exact original backups and compare against those same bytes. When an active install is stale because source assets changed, disable Generate/Install/Modify until safe uninstall; keep Uninstall available when managed hashes match. If installed hashes conflict, disable mutations and show affected files plus backup paths for manual resolution.
- [ ] **Step 4: Run long operations on a worker thread** and deliver progress/results to Tk via a queue polled by `after()`. Disable conflicting controls while work runs and keep all error/rollback summaries visible.
- [ ] **Step 5: Make `w3sub.py` launch the GUI only.** It must not perform an automatic install at startup and must resolve the converter relative to the source or packaged app directory.
- [ ] **Step 6: Run config tests and launch the GUI.** Confirm startup discovery finds the F: Steam install; verify changing the folder triggers a fresh version/language scan; verify unsupported classifier availability disables only dialogue-only mode.

  Run: `python -m unittest tests.test_config -v`

- [ ] **Step 7: Commit** `feat: add desktop interface for dual subtitles`.

### Task 8: User documentation and end-to-end acceptance

**Files:**
- Modify: `README.md`
- Create: `tests/test_workflow.py`

- [ ] **Step 1: Document** Python launch instructions, storefront discovery, supported version checks, language pairing semantics, dialogue-only index limitation, backup location, hash conflicts, update/regenerate workflow, and uninstall behavior.
- [ ] **Step 2: Add a copied-fixture workflow check** covering scan, generation, stale detection, install, modify from originals, and uninstall; never run a destructive lifecycle against the user's live game during this check.
- [ ] **Step 3: Run the full verification set** and manually inspect generated outputs and fixture file hashes.

  Run: `python -m unittest discover -s tests -v`

- [ ] **Step 4: Manually scan the live F: game folder** without installing; confirm version, languages, converter result, storefront label, and exact file inventory. Only install to the live game in a separate user-authorized action.
- [ ] **Step 5: Commit** `docs: describe dual subtitle manager workflow`.
