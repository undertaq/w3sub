"""Tkinter desktop flow for the Witcher 3 dual-subtitle manager."""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import config, generation, install, storefronts
from .converter import CompatibilityReport, W3StringsConverter, check_compatibility
from .w3strings_native import NativeW3StringsCodec
from .dialogue_index import dialogue_index_unavailable_reason
from .game import scan_game
from .models import (
    AppConfig, Freshness, GameCandidate, GameInstallation, GenerationRecord,
    GenerationRequest, InstallComparison, InstallManifest, MergeMode, Storefront,
)


def selected_converter(selection: str):
    """An empty selection uses the built-in codec; paths explicitly override it."""
    return (NativeW3StringsCodec() if selection.strip() in ("", "builtin")
            else W3StringsConverter(Path(selection).expanduser().resolve()))


@dataclass(frozen=True)
class ScannedGame:
    candidate: GameCandidate
    game: GameInstallation

    @property
    def label(self) -> str:
        return f"{self.candidate.storefront.value.upper()} — {self.game.root}"


@dataclass(frozen=True)
class ActionState:
    generate: bool
    install: bool
    modify: bool
    uninstall: bool
    dialogue_mode: bool
    conflict_summary: str = ""
    dialogue_reason: str = ""


@dataclass(frozen=True)
class GameSnapshot:
    selected: ScannedGame
    manifest: InstallManifest | None
    install_comparison: InstallComparison | None
    generation_record: GenerationRecord | None
    generation_freshness: Freshness | None
    dialogue_index_available: bool


def _path_identity(path: Path) -> str:
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(Path(path).expanduser()))
    return os.path.normcase(os.path.normpath(str(resolved)))


def ordered_startup_games(candidates, saved_game_root: Path | None, *, scanner=None,
                          logger: logging.Logger | None = None) -> tuple[ScannedGame, ...]:
    """Scan storefront candidates first; append a valid saved path as fallback."""
    scanner = scanner or scan_game
    logger = logger or logging.getLogger(config._LOGGER_NAME)
    rows = []
    seen = set()
    for candidate in candidates:
        identity = _path_identity(candidate.root)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            game = scanner(candidate.root, candidate.storefront, candidate.store_build_id)
        except Exception as error:
            logger.info("Skipping discovered game candidate %s: %s", candidate.root, error)
            continue
        rows.append(ScannedGame(candidate, game))
    if saved_game_root is not None:
        identity = _path_identity(saved_game_root)
        if not any(_path_identity(row.game.root) == identity for row in rows):
            candidate = GameCandidate(Path(saved_game_root), Storefront.UNKNOWN,
                                      "saved settings", None)
            try:
                game = scanner(candidate.root, candidate.storefront, None)
            except Exception as error:
                logger.info("Saved game folder is no longer usable %s: %s",
                            candidate.root, error)
            else:
                rows.append(ScannedGame(candidate, game))
    return tuple(rows)


def scan_selected_folder(path: Path, *, candidate: GameCandidate | None = None, scanner=None):
    """Rescan a manually or discovery-selected root immediately."""
    scanner = scanner or scan_game
    root = Path(path).expanduser().resolve()
    if candidate is None:
        return scanner(root, Storefront.UNKNOWN, None)
    return scanner(root, candidate.storefront, candidate.store_build_id)


def available_languages(game: GameInstallation) -> tuple[str, ...]:
    """Expose only language codes with scanned resource files."""
    return tuple(sorted(
        language.casefold() for language, paths in game.language_files.items()
        if paths and all(Path(path).is_file() for path in paths)
    ))


def default_language_pair(languages: tuple[str, ...], primary: str = "",
                          secondary: str = "") -> tuple[str, str]:
    choices = tuple(language.casefold() for language in languages)
    if primary.casefold() not in choices:
        primary = "en" if "en" in choices else (choices[0] if choices else "")
    else:
        primary = primary.casefold()
    if secondary.casefold() not in choices or secondary.casefold() == primary:
        secondary = next((code for code in ("zh", "cn", "pl", "fr", "de")
                          if code in choices and code != primary), "")
        if not secondary:
            secondary = next((code for code in choices if code != primary), "")
    else:
        secondary = secondary.casefold()
    return primary, secondary


def source_overrides_for_pair(game: GameInstallation, manifest: InstallManifest | None,
                              primary: str, secondary: str) -> dict[str, Path]:
    """Use validated originals for every selected resource managed by Modify."""
    if manifest is None or not manifest.active:
        return {}
    sources = generation._source_paths(game, primary, secondary, None)
    overrides = {}
    for relative in sorted(sources):
        target = manifest.target_files.get(relative)
        if target is not None:
            overrides[relative] = install._validate_backup(manifest, relative, target)
    return overrides


def check_pair_compatibility(game: GameInstallation, primary: str, secondary: str,
                             converter, app_state: Path,
                             source_overrides: dict[str, Path] | None = None
                             ) -> CompatibilityReport:
    """Round-trip the complete pair inventory and reject concurrent source edits."""
    try:
        sources = generation._source_paths(game, primary, secondary, source_overrides)
        before = generation._fingerprint_sources(sources)
    except Exception as error:
        return CompatibilityReport(False, 0, f"Cannot inspect selected language resources: {error}")
    report = check_compatibility(sources, converter, Path(app_state) / "compatibility")
    try:
        after = generation._fingerprint_sources(sources)
    except Exception as error:
        return CompatibilityReport(False, report.checked_resources,
                                   f"Cannot recheck selected language resources: {error}")
    if before != after:
        return CompatibilityReport(False, report.checked_resources,
                                   "A selected language resource changed during compatibility check")
    return report


def build_action_state(*, has_game: bool, primary: str | None, secondary: str | None,
                       mode: MergeMode, resources_available: bool,
                       converter_compatible: bool, generated: bool,
                       generation_freshness: Freshness | None, install_active: bool,
                       install_comparison: InstallComparison | Freshness | None,
                       conflict_paths: tuple[str, ...], dialogue_index_current: bool
                       ) -> ActionState:
    """Centralize action gates so every lifecycle path fails closed."""
    compare_freshness = getattr(install_comparison, "freshness", install_comparison)
    conflicts = tuple(sorted(set(conflict_paths) |
                             set(getattr(install_comparison, "conflict_paths", ()))))
    active_stale = install_active and compare_freshness not in (
        Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY,
    )
    pair_valid = bool(primary and secondary and primary.casefold() != secondary.casefold())
    mode_ready = mode is MergeMode.FULL_TEXT or dialogue_index_current
    safe_to_generate = not conflicts and not active_stale
    can_generate = bool(has_game and pair_valid and resources_available
                        and converter_compatible and mode_ready and safe_to_generate)
    fresh_generation = generation_freshness in (
        Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY,
    )
    generation_ready = bool(generated and fresh_generation and pair_valid)
    can_install = bool(has_game and generation_ready and not install_active
                       and not conflicts and not active_stale)
    can_modify = bool(has_game and generation_ready and install_active
                      and not conflicts and not active_stale)
    can_uninstall = bool(has_game and install_active and not conflicts)
    reason = ("No validated structured dialogue-reference index is available; "
              "dialogue-only mode is disabled. Full-text mode remains available.")
    return ActionState(can_generate, can_install, can_modify, can_uninstall,
                       bool(has_game and dialogue_index_current), "\n".join(conflicts), reason)


def default_converter_path() -> Path:
    """Resolve the bundled converter next to the source or packaged application."""
    if getattr(sys, "frozen", False):
        executable_path = Path(sys.executable).resolve().parent / "w3strings.exe"
        if executable_path.is_file():
            return executable_path
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            bundled_path = Path(bundle_root).resolve() / "w3strings.exe"
            if bundled_path.is_file():
                return bundled_path
        return executable_path
    return Path(__file__).resolve().parent.parent / "w3strings.exe"


def confirmation_text(operation: str, game_root: Path, primary: str, secondary: str,
                      target_paths: tuple[str, ...], backup_directory: Path | None = None) -> str:
    action = "Restore" if operation.casefold() == "uninstall" else operation
    lines = [f"{action} {primary} + {secondary} for this game folder?", str(game_root), "",
             "Exact target files:"]
    lines.extend(f"  {path}" for path in sorted(target_paths))
    if backup_directory is not None:
        lines.extend(("", f"Original backups: {backup_directory}"))
    return "\n".join(lines)


def completed_paths_text(game_root: Path, relative_paths: tuple[str, ...],
                         backup_directory: Path | None) -> str:
    targets = sorted(str(Path(game_root).joinpath(*Path(relative).parts))
                     for relative in relative_paths)
    lines = ["Completed paths:"]
    lines.extend(f"  {target}" for target in targets)
    if not targets:
        lines.append("  (none)")
    if backup_directory is not None:
        lines.extend(("", f"Original backups remain at {backup_directory}"))
    return "\n".join(lines)


def confirm_then_submit(confirm, title: str, text: str, submit) -> bool:
    """Do not launch a worker or mutate state if the user cancels."""
    if confirm(title, text) is not True:
        return False
    submit()
    return True


def install_manifest_review_signature(manifest: InstallManifest) -> tuple:
    """Identify the active install targets and backups shown in a confirmation."""
    targets = tuple(sorted(
        (
            relative,
            target.relative_path,
            _path_identity(target.backup_path),
            target.original_sha256,
            target.installed_sha256,
        )
        for relative, target in manifest.target_files.items()
    ))
    return (
        _path_identity(manifest.game_root),
        _path_identity(manifest.state_directory),
        _path_identity(manifest.backup_directory),
        manifest.storefront,
        manifest.store_build_id,
        manifest.install_id,
        manifest.generation_id,
        manifest.generation_version,
        manifest.install_version,
        manifest.primary_language,
        manifest.secondary_language,
        manifest.mode,
        manifest.source_fingerprint.digest,
        targets,
        manifest.active,
        manifest.conflicted,
        manifest.prepared,
    )


def require_reviewed_manifest(signature: tuple, manifest: InstallManifest | None
                               ) -> InstallManifest:
    """Refuse a lifecycle action if its confirmed install record has changed."""
    if manifest is None or install_manifest_review_signature(manifest) != signature:
        raise install.InstallError(
            "The install changed after confirmation. Rescan the game and review the current "
            "targets before confirming again."
        )
    return manifest


def summarize_compatibility_error(error: str | None) -> str:
    detail = (error or "converter failed compatibility check").strip()
    match = re.search(
        r"supported versions:\s*(\d+)\s*-\s*(\d+)\.\s*found:\s*(\d+)",
        detail, re.IGNORECASE,
    )
    log_path = config.app_state_root() / "w3dual-subtitle.log"
    if match:
        low, high, found = match.groups()
        return (f"Converter does not support format {found}; it accepts {low}–{high}. "
                f"Select a compatible converter. Full diagnostics: {log_path}")
    first_line = detail.splitlines()[0] if detail else "Converter check failed"
    first_line = first_line[:200]
    failed_resources = sum(1 for line in detail.splitlines()
                           if ".w3strings:" in line and ("failed" in line or "changed" in line))
    count = f"{failed_resources} resource(s) failed. " if failed_resources else ""
    return f"{count}{first_line}. Full diagnostics: {log_path}"


def start_background_operation(messages: queue.Queue, label: str, operation,
                               on_success, logger: logging.Logger | None = None):
    """Run work away from Tk and post one result for the UI's after() poller."""
    logger = logger or logging.getLogger(config._LOGGER_NAME)

    def worker():
        try:
            result = operation()
        except Exception as error:
            logger.exception("Background operation %s failed", label)
            messages.put((label, None, error))
        else:
            messages.put((label, (result, on_success), None))

    thread_name = re.sub(r"\s+", "-", label.strip()) or "operation"
    thread = threading.Thread(target=worker, name=f"w3sub-{thread_name}", daemon=True)
    thread.start()
    return thread


class W3DualSubtitleApp:
    def __init__(self, root: tk.Tk, app_config: AppConfig | None = None):
        self.root = root
        self.logger = config.configure_logging()
        self.app_config = app_config or config.load_config()
        self.app_root = config.app_state_root().resolve()
        self.converter_path = self.app_config.converter_path
        self._messages: queue.Queue = queue.Queue()
        self._busy = False
        self._progress_running = False
        self._candidate_rows: tuple[ScannedGame, ...] = ()
        self.snapshot: GameSnapshot | None = None
        self.converter_compatible = False
        self._allow_game_value_event = False
        self._build_widgets()
        self.root.title("Witcher 3 Dual Subtitle Manager")
        self.root.minsize(760, 630)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(75, self._poll_messages)
        self._submit("startup", self._startup_scan, self._startup_loaded)

    def _build_widgets(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)

        ttk.Label(frame, text="Game installation").grid(row=0, column=0, sticky="w")
        self.game_var = tk.StringVar()
        self.game_combo = ttk.Combobox(frame, textvariable=self.game_var, state="readonly")
        self.game_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        self.game_combo.bind("<<ComboboxSelected>>", self._candidate_changed)
        self.browse_button = ttk.Button(frame, text="Browse…", command=self._browse_game)
        self.browse_button.grid(row=0, column=2, sticky="ew")

        self.game_details_var = tk.StringVar(value="Scanning storefront records…")
        ttk.Label(frame, textvariable=self.game_details_var, wraplength=780).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(frame, text="Codec / external override").grid(row=2, column=0, sticky="w")
        self.converter_var = tk.StringVar(value=str(self.converter_path) if self.converter_path else "builtin")
        self.converter_entry = ttk.Entry(frame, textvariable=self.converter_var)
        self.converter_entry.configure(state="readonly")
        self.converter_entry.grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        self.converter_button = ttk.Button(frame, text="Browse…", command=self._browse_converter)
        self.converter_button.grid(row=2, column=2, sticky="ew")
        self.native_codec_button = ttk.Button(frame, text="Use built-in", command=self._use_native_codec)
        self.native_codec_button.grid(row=2, column=3, sticky="ew", padx=(6, 0))
        self.converter_status_var = tk.StringVar(value="Compatibility has not been checked")
        ttk.Label(frame, textvariable=self.converter_status_var, wraplength=780).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(0, 5))

        pair = ttk.Frame(frame)
        pair.grid(row=4, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(pair, text="Primary language").pack(side="left")
        self.primary_var = tk.StringVar()
        self.primary_combo = ttk.Combobox(pair, textvariable=self.primary_var,
                                          state="readonly", width=9)
        self.primary_combo.pack(side="left", padx=(6, 18))
        self.primary_combo.bind("<<ComboboxSelected>>", self._pair_changed)
        ttk.Label(pair, text="Secondary language").pack(side="left")
        self.secondary_var = tk.StringVar()
        self.secondary_combo = ttk.Combobox(pair, textvariable=self.secondary_var,
                                            state="readonly", width=9)
        self.secondary_combo.pack(side="left", padx=6)
        self.secondary_combo.bind("<<ComboboxSelected>>", self._pair_changed)

        mode = ttk.Frame(frame)
        mode.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
        self.mode_var = tk.StringVar(value=MergeMode.FULL_TEXT.value)
        self.full_mode = ttk.Radiobutton(
            mode, text="Full-text merge", variable=self.mode_var,
            value=MergeMode.FULL_TEXT.value, command=self._mode_changed,
        )
        self.full_mode.pack(side="left")
        self.dialogue_mode = ttk.Radiobutton(
            mode, text="Dialogue-only merge", variable=self.mode_var,
            value=MergeMode.DIALOGUE_ONLY.value, command=self._mode_changed,
        )
        self.dialogue_mode.pack(side="left", padx=12)
        self.mode_reason_var = tk.StringVar()
        ttk.Label(mode, textvariable=self.mode_reason_var, wraplength=450).pack(
            side="left", fill="x", expand=True)

        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(5, 7))
        self.generate_button = ttk.Button(actions, text="Generate preview", command=self._generate_preview)
        self.generate_button.pack(side="left", padx=(0, 6))
        self.install_button = ttk.Button(actions, text="Install", command=self._install)
        self.install_button.pack(side="left", padx=6)
        self.modify_button = ttk.Button(actions, text="Modify install", command=self._modify)
        self.modify_button.pack(side="left", padx=6)
        self.uninstall_button = ttk.Button(actions, text="Uninstall", command=self._uninstall)
        self.uninstall_button.pack(side="left", padx=6)
        self.rescan_button = ttk.Button(actions, text="Rescan", command=self._rescan_current)
        self.rescan_button.pack(side="right")

        ttk.Label(frame, text="Generation preview — target path and SHA-256").grid(
            row=7, column=0, columnspan=3, sticky="w")
        preview_frame = ttk.Frame(frame)
        preview_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview = ttk.Treeview(preview_frame, columns=("hash",), show="headings", height=8)
        self.preview.heading("hash", text="Target resource — SHA-256")
        self.preview.column("hash", width=710, anchor="w")
        self.preview.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.preview.configure(yscrollcommand=scroll.set)
        self.status_var = tk.StringVar(value="Ready")
        self.operation_progress = ttk.Progressbar(frame, mode="indeterminate")
        self.operation_progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Label(frame, textvariable=self.status_var, wraplength=800).grid(
            row=10, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._set_controls_enabled(False)

    def _sync_operation_progress(self):
        if self._busy and not self._progress_running:
            self.operation_progress.start()
            self._progress_running = True
        elif not self._busy and self._progress_running:
            self.operation_progress.stop()
            self._progress_running = False

    def _on_close(self):
        if self._busy:
            self.status_var.set(
                "An operation is still running. Wait for it to finish before closing the app."
            )
            return False
        self.root.destroy()
        return True

    def _submit(self, label, operation, on_success):
        if self._busy:
            return
        self._busy = True
        self._sync_operation_progress()
        self.status_var.set(f"{label.capitalize()}…")
        self._set_controls_enabled(False)
        self._refresh_action_buttons()

        start_background_operation(self._messages, label, operation, on_success, self.logger)

    def _poll_messages(self):
        try:
            while True:
                label, payload, error = self._messages.get_nowait()
                self._busy = False
                if error is not None:
                    details = [str(error)]
                    target_paths = getattr(error, "target_paths", ())
                    rollback_errors = getattr(error, "rollback_errors", ())
                    backup = getattr(error, "backup_directory", None)
                    if target_paths:
                        details.append("Affected files: " + ", ".join(target_paths))
                    if backup:
                        details.append(f"Original backups: {backup}")
                    if rollback_errors:
                        details.append("Rollback diagnostics: " + "; ".join(rollback_errors))
                    self.status_var.set(f"{label.capitalize()} failed: " + " | ".join(details))
                    if label == "converter compatibility check":
                        self.converter_compatible = False
                        self.converter_status_var.set(
                            "Unavailable: " + summarize_compatibility_error(str(error))
                        )
                else:
                    result, callback = payload
                    callback(result)
                self._sync_operation_progress()
                self._set_controls_enabled(not self._busy)
                self._refresh_action_buttons()
        except queue.Empty:
            pass
        self.root.after(75, self._poll_messages)

    def _set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.game_combo, self.browse_button, self.converter_entry,
                       self.converter_button, self.native_codec_button, self.primary_combo, self.secondary_combo,
                       self.full_mode, self.dialogue_mode, self.rescan_button):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        if enabled:
            self.game_combo.configure(state="readonly")
            self.primary_combo.configure(state="readonly")
            self.secondary_combo.configure(state="readonly")
            self.converter_entry.configure(state="readonly")
            if not self.snapshot or not self.snapshot.dialogue_index_available:
                self.dialogue_mode.configure(state="disabled")

    def _startup_scan(self):
        candidates = storefronts.discover_candidates()
        rows = ordered_startup_games(candidates, self.app_config.last_game_root,
                                     logger=self.logger)
        if not rows:
            return rows, None
        return rows, self._load_game_snapshot(rows[0])

    def _startup_loaded(self, result):
        rows, snapshot = result
        self._candidate_rows = rows
        self._set_candidate_values()
        if snapshot is None:
            self.game_details_var.set(
                "No valid storefront install was found. Browse to a game folder to scan it."
            )
            self.status_var.set("No installation found; manual folder selection is available")
            return
        self._apply_snapshot(snapshot)
        self._run_compatibility_check()

    def _set_candidate_values(self):
        self._allow_game_value_event = True
        values = [row.label for row in self._candidate_rows]
        self.game_combo.configure(values=values)
        if self.snapshot:
            self.game_var.set(self.snapshot.selected.label)
        elif values:
            self.game_var.set(values[0])
        self._allow_game_value_event = False

    def _load_game_snapshot(self, selected: ScannedGame) -> GameSnapshot:
        game = selected.game
        state_root = config.state_root_for(game.root)
        manifest = install.load_install_manifest(state_root, game.root)
        comparison = install.compare_install(manifest, game) if manifest else None
        record = generation.load_latest_generation_record(self.app_root, game.root)
        freshness = None
        if record is not None:
            try:
                overrides = source_overrides_for_pair(
                    game, manifest, record.primary_language, record.secondary_language,
                )
                freshness = generation.compare_generation(record, game, overrides or None)
            except Exception:
                freshness = Freshness.STALE
        dialogue_index = generation._current_dialogue_index(game)
        return GameSnapshot(selected, manifest, comparison, record, freshness,
                            dialogue_index is not None)

    def _apply_snapshot(self, snapshot: GameSnapshot):
        self.snapshot = snapshot
        self.app_config = AppConfig(snapshot.selected.game.root, self.app_config.converter_path)
        config.save_config(self.app_config)
        game = snapshot.selected.game
        version = game.version
        languages = available_languages(game)
        build = f"; store build {version.store_build_id}" if version.store_build_id else ""
        raw = version.executable_version_raw or version.executable_version
        self.game_details_var.set(
            f"{game.root}\n{snapshot.selected.candidate.storefront.value.upper()} — "
            f"executable {version.executable_version} ({raw}){build}; "
            f"languages: {', '.join(languages) if languages else 'none'}"
        )
        self.primary_combo.configure(values=languages)
        self.secondary_combo.configure(values=languages)
        primary, secondary = default_language_pair(
            languages, self.primary_var.get(), self.secondary_var.get(),
        )
        self.primary_var.set(primary)
        self.secondary_var.set(secondary)
        self.mode_reason_var.set(
            "" if snapshot.dialogue_index_available else dialogue_index_unavailable_reason(game)
        )
        self.dialogue_mode.configure(state="normal" if snapshot.dialogue_index_available else "disabled")
        if not snapshot.dialogue_index_available and self.mode_var.get() == MergeMode.DIALOGUE_ONLY.value:
            self.mode_var.set(MergeMode.FULL_TEXT.value)
        self._render_generation_and_install()
        self._set_candidate_values()
        self._refresh_action_buttons()

    def _render_generation_and_install(self):
        for item in self.preview.get_children():
            self.preview.delete(item)
        snapshot = self.snapshot
        if snapshot and snapshot.generation_record:
            record = snapshot.generation_record
            freshness_label = (snapshot.generation_freshness.value.replace("_", " ")
                               if snapshot.generation_freshness else "unknown")
            for relative, digest in sorted(record.output_hashes.items()):
                target = snapshot.selected.game.root.joinpath(*Path(relative).parts)
                self.preview.insert("", "end", values=(f"{target} — {digest}",))
            manifest = snapshot.manifest
            if manifest and manifest.active:
                for relative, target_record in sorted(manifest.target_files.items()):
                    if relative not in record.output_files:
                        target = snapshot.selected.game.root.joinpath(*Path(relative).parts)
                        self.preview.insert(
                            "", "end",
                            values=(f"RESTORE original: {target} — {target_record.original_sha256}",),
                        )
            self.status_var.set(
                f"Generation {record.generation_id}: "
                f"{freshness_label}; "
                f"{record.primary_language} + {record.secondary_language}; {record.mode.value}"
            )
        if snapshot and snapshot.manifest and snapshot.manifest.active:
            manifest = snapshot.manifest
            comparison = snapshot.install_comparison
            if comparison and comparison.conflict_paths:
                self.status_var.set(
                    f"Install conflicts: {', '.join(comparison.conflict_paths)}; "
                    f"originals backed up at {manifest.backup_directory}"
                )
            else:
                freshness = (comparison.freshness.value.replace("_", " ")
                             if comparison else "unknown")
                self.status_var.set(
                    f"Active install {manifest.primary_language} + {manifest.secondary_language}; "
                    f"{freshness}; originals at {manifest.backup_directory}"
                )

    def _candidate_changed(self, _event=None):
        if self._allow_game_value_event or self._busy:
            return
        selected = next((row for row in self._candidate_rows
                         if row.label == self.game_var.get()), None)
        if selected is not None:
            self._rescan_to_candidate(selected)

    def _rescan_to_candidate(self, selected: ScannedGame):
        def operation():
            game = scan_selected_folder(selected.candidate.root, candidate=selected.candidate)
            refreshed = ScannedGame(selected.candidate, game)
            return refreshed, self._load_game_snapshot(refreshed)

        self._submit("rescan selected folder", operation, self._selection_loaded)

    def _selection_loaded(self, result):
        selected, snapshot = result
        rows = [row for row in self._candidate_rows
                if _path_identity(row.game.root) != _path_identity(selected.game.root)]
        self._candidate_rows = (selected, *rows)
        self._apply_snapshot(snapshot)
        self.converter_compatible = False
        self._run_compatibility_check()

    def _browse_game(self):
        chosen = filedialog.askdirectory(title="Select The Witcher 3 game folder")
        if not chosen:
            return
        candidate = GameCandidate(Path(chosen), Storefront.UNKNOWN, "manual selection")

        def operation():
            game = scan_selected_folder(candidate.root, candidate=candidate)
            selected = ScannedGame(candidate, game)
            return selected, self._load_game_snapshot(selected)

        self._submit("scan selected folder", operation,
                     lambda result: self._selection_loaded(result))

    def _browse_converter(self):
        chosen = filedialog.askopenfilename(
            title="Select a compatible w3strings converter",
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")),
        )
        if not chosen:
            return
        self.converter_path = Path(chosen).resolve()
        self.converter_var.set(str(self.converter_path))
        game_root = self.snapshot.selected.game.root if self.snapshot else self.app_config.last_game_root
        self.app_config = AppConfig(game_root, self.converter_path)
        config.save_config(self.app_config)
        self.converter_compatible = False
        self._run_compatibility_check()

    def _use_native_codec(self):
        self.converter_path = None
        self.converter_var.set("builtin")
        game_root = self.snapshot.selected.game.root if self.snapshot else self.app_config.last_game_root
        self.app_config = AppConfig(game_root, None)
        config.save_config(self.app_config)
        self.converter_compatible = False
        self._run_compatibility_check()

    def _pair_changed(self, _event=None):
        if self._busy:
            return
        self.converter_compatible = False
        self._refresh_action_buttons()
        self._run_compatibility_check()

    def _mode_changed(self):
        self._refresh_action_buttons()

    def _run_compatibility_check(self):
        if not self.snapshot or not self.primary_var.get() or not self.secondary_var.get():
            self.converter_status_var.set("Choose two available, different languages")
            return
        game = self.snapshot.selected.game
        primary, secondary = self.primary_var.get(), self.secondary_var.get()
        if primary.casefold() == secondary.casefold():
            self.converter_status_var.set("Primary and secondary languages must differ")
            return
        manifest = self.snapshot.manifest
        converter_raw = self.converter_var.get().strip()

        def operation():
            converter = selected_converter(converter_raw)
            overrides = source_overrides_for_pair(game, manifest, primary, secondary)
            return check_pair_compatibility(game, primary, secondary, converter,
                                            self.app_root, overrides or None)

        self.converter_status_var.set("Checking selected resource copies with the selected codec…")
        self._submit("converter compatibility check", operation, self._compatibility_checked)

    def _compatibility_checked(self, report: CompatibilityReport):
        self.converter_compatible = report.compatible
        if report.compatible:
            self.converter_status_var.set(
                f"Compatible: {report.checked_resources} selected resources passed semantic round-trip — "
                f"{self.converter_var.get()}"
            )
        else:
            detail = report.error or "converter failed compatibility check"
            self.converter_status_var.set(
                "Unavailable: " + summarize_compatibility_error(detail)
            )
            self.logger.warning("Converter compatibility failed for %s: %s",
                                self.converter_var.get(), detail)
        self._refresh_action_buttons()

    def _resources_pairable(self):
        if not self.snapshot:
            return False
        try:
            generation._pair_resources(
                self.snapshot.selected.game, self.primary_var.get(), self.secondary_var.get(),
            )
            return True
        except Exception:
            return False

    def _refresh_action_buttons(self):
        snapshot = self.snapshot
        active = bool(snapshot and snapshot.manifest and snapshot.manifest.active)
        comparison = snapshot.install_comparison if snapshot else None
        conflicts = comparison.conflict_paths if comparison else ()
        record = snapshot.generation_record if snapshot else None
        pair_matches = bool(
            record and record.primary_language == self.primary_var.get().casefold()
            and record.secondary_language == self.secondary_var.get().casefold()
            and record.mode.value == self.mode_var.get()
        )
        if active and snapshot and snapshot.manifest and record:
            pair_matches = pair_matches and record.generation_id != snapshot.manifest.generation_id
        actions = build_action_state(
            has_game=snapshot is not None,
            primary=self.primary_var.get(), secondary=self.secondary_var.get(),
            mode=MergeMode(self.mode_var.get()), resources_available=self._resources_pairable(),
            converter_compatible=self.converter_compatible,
            generated=bool(record and pair_matches),
            generation_freshness=snapshot.generation_freshness if snapshot else None,
            install_active=active, install_comparison=comparison,
            conflict_paths=conflicts,
            dialogue_index_current=bool(snapshot and snapshot.dialogue_index_available),
        )
        pairable = self._resources_pairable()
        self.full_mode.configure(state="normal" if pairable and not self._busy else "disabled")
        self.dialogue_mode.configure(
            state="normal" if pairable and snapshot and snapshot.dialogue_index_available
            and not self._busy else "disabled"
        )
        for button, enabled in (
            (self.generate_button, actions.generate), (self.install_button, actions.install),
            (self.modify_button, actions.modify), (self.uninstall_button, actions.uninstall),
        ):
            button.configure(state="normal" if enabled and not self._busy else "disabled")
        if not actions.dialogue_mode:
            self.dialogue_mode.configure(state="disabled")

    def _fresh_game_and_manifest(self, allow_stale_install=False,
                                 reviewed_manifest_signature=None):
        if not self.snapshot:
            raise RuntimeError("Select a game folder first")
        selected = self.snapshot.selected
        game = scan_game(selected.game.root, selected.candidate.storefront,
                         selected.candidate.store_build_id)
        state = config.state_root_for(game.root)
        manifest = install.load_install_manifest(state, game.root)
        if reviewed_manifest_signature is not None:
            manifest = require_reviewed_manifest(reviewed_manifest_signature, manifest)
        comparison = install.compare_install(manifest, game) if manifest else None
        if manifest and manifest.active:
            if comparison and comparison.conflict_paths:
                raise install.InstallError(
                    "Managed files or original backups conflict; resolve these exact paths before continuing: "
                    + ", ".join(comparison.conflict_paths),
                    target_paths=comparison.conflict_paths,
                    backup_directory=manifest.backup_directory,
                )
            if (not allow_stale_install and comparison and comparison.freshness not in (
                    Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY)):
                raise install.InstallError(
                    f"Active install is {comparison.freshness.value}; safely uninstall it before regenerating"
                )
        return game, state, manifest, comparison

    def _generate_preview(self):
        if not self.snapshot:
            return
        primary, secondary = self.primary_var.get(), self.secondary_var.get()
        mode = MergeMode(self.mode_var.get())
        converter_raw = self.converter_var.get().strip()

        def operation():
            game, _state, manifest, _comparison = self._fresh_game_and_manifest()
            overrides = source_overrides_for_pair(game, manifest, primary, secondary)
            converter = selected_converter(converter_raw)
            compatibility = check_pair_compatibility(
                game, primary, secondary, converter, self.app_root, overrides or None,
            )
            if not compatibility.compatible:
                self.logger.warning("Generation converter preflight failed: %s", compatibility.error)
                raise generation.GenerationError(
                    "Converter is incompatible with selected inputs: "
                    + summarize_compatibility_error(compatibility.error)
                )
            record = generation.generate(
                GenerationRequest(game, primary, secondary, mode, overrides or None),
                self.app_root, converter,
            )
            freshness = generation.compare_generation(record, game, overrides or None)
            return record, freshness

        self._submit("generate preview", operation, self._preview_generated)

    def _preview_generated(self, result):
        record, freshness = result
        if self.snapshot:
            self.snapshot = GameSnapshot(
                self.snapshot.selected, self.snapshot.manifest, self.snapshot.install_comparison,
                record, freshness, self.snapshot.dialogue_index_available,
            )
        self._render_generation_and_install()
        self.status_var.set(
            f"Preview ready: {len(record.output_files)} target files; source freshness {freshness.value}. "
            "Review paths and hashes before Install or Modify."
        )
        self._refresh_action_buttons()

    def _install(self):
        if not self.snapshot or not self.snapshot.generation_record:
            return
        record = self.snapshot.generation_record

        def operation():
            game, state, manifest, _comparison = self._fresh_game_and_manifest()
            if manifest and manifest.active:
                raise install.InstallError("An install is already active; use Modify or Uninstall")
            freshness = generation.compare_generation(record, game)
            if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
                raise install.InstallError(f"Preview is {freshness.value}; regenerate before install")
            result = install.install_generation(game, record, state)
            refreshed = ScannedGame(self.snapshot.selected.candidate, game)
            return result, self._load_game_snapshot(refreshed), tuple(record.output_files)

        root = self.snapshot.selected.game.root
        targets = tuple(str(root.joinpath(*Path(path).parts)) for path in record.output_files)
        text = confirmation_text("Install", root, record.primary_language,
                                 record.secondary_language, targets)
        confirm_then_submit(
            messagebox.askyesno, "Confirm dual subtitle install", text,
            lambda: self._submit("install", operation, self._lifecycle_completed),
        )

    def _modify(self):
        if not self.snapshot or not self.snapshot.generation_record:
            return
        record = self.snapshot.generation_record
        reviewed_manifest = self.snapshot.manifest
        if reviewed_manifest is None:
            return
        reviewed_signature = install_manifest_review_signature(reviewed_manifest)

        def operation():
            game, _state, manifest, _comparison = self._fresh_game_and_manifest(
                reviewed_manifest_signature=reviewed_signature,
            )
            if manifest is None or not manifest.active:
                raise install.InstallError("There is no active install to modify")
            overrides = source_overrides_for_pair(
                game, manifest, record.primary_language, record.secondary_language,
            )
            freshness = generation.compare_generation(record, game, overrides or None)
            if freshness not in (Freshness.CURRENT, Freshness.VERSION_METADATA_CHANGED_ONLY):
                raise install.InstallError(f"Preview is {freshness.value}; regenerate before Modify")
            result = install.modify_install(game, record, manifest)
            refreshed = ScannedGame(self.snapshot.selected.candidate, game)
            completed = tuple(set(record.output_files) | set(manifest.target_files))
            return result, self._load_game_snapshot(refreshed), completed

        root = self.snapshot.selected.game.root
        relative_targets = set(record.output_files)
        relative_targets.update(reviewed_manifest.target_files)
        targets = tuple(str(root.joinpath(*Path(path).parts)) for path in relative_targets)
        text = confirmation_text("Modify", root, record.primary_language,
                                 record.secondary_language, targets)
        confirm_then_submit(
            messagebox.askyesno, "Confirm dual subtitle modification", text,
            lambda: self._submit("modify install", operation, self._lifecycle_completed),
        )

    def _uninstall(self):
        if not self.snapshot or not self.snapshot.manifest:
            return
        reviewed_manifest = self.snapshot.manifest
        reviewed_signature = install_manifest_review_signature(reviewed_manifest)

        def operation():
            game, _state, manifest, comparison = self._fresh_game_and_manifest(
                allow_stale_install=True,
                reviewed_manifest_signature=reviewed_signature,
            )
            if manifest is None or not manifest.active:
                raise install.InstallError("There is no active install to uninstall")
            if comparison and comparison.conflict_paths:
                raise install.InstallError(
                    "Uninstall is unsafe while these managed files or backups conflict: "
                    + ", ".join(comparison.conflict_paths),
                    target_paths=comparison.conflict_paths,
                    backup_directory=manifest.backup_directory,
                )
            result = install.uninstall(game, manifest)
            if result.conflicts or result.error:
                raise install.InstallError(
                    result.error or "Uninstall stopped because managed files conflict",
                    target_paths=result.conflicts, rollback_errors=result.rollback_errors,
                    backup_directory=result.backup_directory,
                )
            refreshed = ScannedGame(self.snapshot.selected.candidate, game)
            return result, self._load_game_snapshot(refreshed), result.restored_paths

        manifest = reviewed_manifest
        root = self.snapshot.selected.game.root
        targets = tuple(str(root.joinpath(*Path(path).parts))
                        for path in manifest.target_files)
        text = confirmation_text(
            "Uninstall", root, manifest.primary_language, manifest.secondary_language,
            targets, manifest.backup_directory,
        )
        confirm_then_submit(
            messagebox.askyesno, "Confirm dual subtitle removal", text,
            lambda: self._submit("uninstall", operation, self._lifecycle_completed),
        )

    def _lifecycle_completed(self, result):
        operation_result, snapshot, completed_paths = result
        self._apply_snapshot(snapshot)
        self.status_var.set(completed_paths_text(
            snapshot.selected.game.root, completed_paths,
            getattr(operation_result, "backup_directory", None),
        ))
        self._refresh_action_buttons()

    def _rescan_current(self):
        if self.snapshot is None:
            self._submit("startup scan", self._startup_scan, self._startup_loaded)
        else:
            self._rescan_to_candidate(self.snapshot.selected)


def run() -> None:
    """Launch the GUI only; launch never scans into or changes game files."""
    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise RuntimeError(f"Cannot open the desktop interface: {error}") from error
    W3DualSubtitleApp(root)
    root.mainloop()
