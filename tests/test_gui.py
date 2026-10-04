from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest


class NativeSelectionTests(unittest.TestCase):
    def test_reset_to_native_clears_persisted_external_override(self):
        from types import SimpleNamespace
        from w3sub_app.gui import W3DualSubtitleApp
        from w3sub_app.models import AppConfig
        value = []
        checked = []
        app = SimpleNamespace(snapshot=None, app_config=AppConfig(None, Path("external.exe")),
                              converter_var=SimpleNamespace(set=value.append),
                              _run_compatibility_check=lambda: checked.append(True))
        with patch("w3sub_app.gui.config.save_config") as save:
            W3DualSubtitleApp._use_native_codec(app)
        self.assertIsNone(app.converter_path)
        self.assertIsNone(app.app_config.converter_path)
        self.assertEqual(value, ["builtin"])
        self.assertEqual(checked, [True])
        save.assert_called_once_with(AppConfig())

    def test_builtin_is_default_and_external_requires_selection(self):
        from w3sub_app.gui import selected_converter
        from w3sub_app.converter import W3StringsConverter
        from w3sub_app.w3strings_native import NativeW3StringsCodec
        self.assertIsInstance(selected_converter(""), NativeW3StringsCodec)
        self.assertIsInstance(selected_converter("builtin"), NativeW3StringsCodec)
        self.assertIsInstance(selected_converter("custom.exe"), W3StringsConverter)
from unittest.mock import patch

import w3sub_app.gui as gui_module
from w3sub_app.converter import check_compatibility
from w3sub_app.gui import (
    available_languages,
    build_action_state,
    confirmation_text,
    confirm_then_submit,
    completed_paths_text,
    ordered_startup_games,
    default_language_pair,
    default_converter_path,
    summarize_compatibility_error,
    start_background_operation,
    scan_selected_folder,
    source_overrides_for_pair,
)
from w3sub_app.generation import generate, load_latest_generation_record
from w3sub_app.install import InstallError, install_generation
from w3sub_app.models import (
    Freshness,
    GameCandidate,
    GameInstallation,
    GameVersion,
    GenerationRequest,
    MergeMode,
    Storefront,
)


class FixtureConverter:
    version = "fixture converter"

    def __init__(self, executable, *, fail_on=None, corrupt_roundtrip=False, mutate_input=False):
        self.executable = Path(executable)
        self.fail_on = fail_on
        self.corrupt_roundtrip = corrupt_roundtrip
        self.mutate_input = mutate_input
        self.decoded = []

    def decode(self, source, work_dir):
        source = Path(source)
        if self.fail_on and self.fail_on in source.name:
            raise RuntimeError("format 164 is unsupported")
        self.decoded.append(source.name)
        payload = source.read_bytes()
        if self.mutate_input:
            source.write_bytes(b"converter mutation")
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        result = work_dir / f"decoded-{len(self.decoded)}.csv"
        if self.corrupt_roundtrip and len(self.decoded) == 2:
            payload = b"1|00000001||changed\n"
        result.write_bytes(payload)
        return result

    def encode(self, csv_path, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        output = work_dir / "roundtrip.w3strings"
        output.write_bytes(Path(csv_path).read_bytes())
        return output


class GuiHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="w3sub gui ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        rescan = patch("w3sub_app.install._rescan_game", side_effect=lambda game: game, create=True)
        rescan.start()
        self.addCleanup(rescan.stop)

    def test_rescan_refreshes_storefront_build_for_canonical_root(self):
        from w3sub_app import gui as gui_module
        game = self.make_game("rescan metadata")
        candidate = GameCandidate(game.root, game.storefront, "cached", game.version.store_build_id)
        fresh_candidate = replace(candidate, root=game.root / ".", store_build_id="build-new")
        selected = gui_module.ScannedGame(candidate, game)
        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        captured = []
        app._submit = lambda label, operation, callback: captured.append(operation())
        app._load_game_snapshot = lambda scanned: scanned.game
        app._selection_loaded = lambda result: None
        def scanner(root, storefront, build):
            return replace(game, version=replace(game.version, store_build_id=build))
        with patch("w3sub_app.gui.storefronts.discover_candidates", return_value=[fresh_candidate]), \
             patch("w3sub_app.gui.scan_game", side_effect=scanner):
            app._rescan_to_candidate(selected)
        self.assertEqual(captured[0][0].game.version.store_build_id, "build-new")
        self.assertEqual(captured[0][0].candidate.store_build_id, "build-new")

    def test_lifecycle_preflight_refreshes_storefront_build_metadata(self):
        from w3sub_app import gui as gui_module
        game = self.make_game("lifecycle metadata")
        candidate = GameCandidate(game.root, game.storefront, "cached", game.version.store_build_id)
        fresh_candidate = replace(candidate, store_build_id="build-new")
        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        app.snapshot = gui_module.GameSnapshot(gui_module.ScannedGame(candidate, game), None, None, None, None, False)
        def scanner(root, storefront, build):
            return replace(game, version=replace(game.version, store_build_id=build))
        with patch("w3sub_app.gui.storefronts.discover_candidates", return_value=[fresh_candidate]), \
             patch("w3sub_app.gui.scan_game", side_effect=scanner), \
             patch("w3sub_app.gui.install.load_install_manifest", return_value=None):
            refreshed, _, _, _ = app._fresh_game_and_manifest()
        self.assertEqual(refreshed.version.store_build_id, "build-new")

    def test_missing_storefront_record_does_not_reuse_cached_build(self):
        from w3sub_app import storefronts
        game = self.make_game("missing metadata")
        candidate = GameCandidate(game.root, game.storefront, "cached", "old-build")
        with patch.object(storefronts, "discover_candidates", return_value=[]):
            self.assertIsNone(storefronts.refresh_candidate(candidate).store_build_id)


    def make_game(self, name, languages=("en", "zh")):
        root = self.base / name
        files = {language: [] for language in languages}
        for language in languages:
            path = root / "content" / "content0" / f"{language}.w3strings"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"1|00000001||{language} line\n", encoding="utf-8")
            files[language].append(path)
        return GameInstallation(
            root, Storefront.STEAM,
            GameVersion("5.0.0.1", "fixture", "5.0.0.1"),
            {language: tuple(paths) for language, paths in files.items()},
        )

    def candidate(self, game, label="registry fixture"):
        return GameCandidate(game.root, game.storefront, label, game.version.store_build_id)

    def index_app(self, game):
        """Exercise Tk callbacks with simple variables/widgets and a real queue."""
        import queue
        from types import SimpleNamespace

        class Variable:
            def __init__(self, value=""):
                self.value = value
            def get(self):
                return self.value
            def set(self, value):
                self.value = value

        class Widget:
            def __init__(self):
                self.options = {}
            def configure(self, **options):
                self.options.update(options)
            def start(self):
                pass
            def stop(self):
                pass

        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        selected = gui_module.ScannedGame(self.candidate(game), game)
        app.snapshot = gui_module.GameSnapshot(selected, None, None, None, None, False)
        app.app_config = gui_module.AppConfig()
        app.app_root = self.base / "app-state"
        app._busy = False
        app._progress_running = False
        app._messages = queue.Queue()
        app.logger = None
        app.root = SimpleNamespace(after=lambda *_args: None)
        app.converter_compatible = True
        for name in ("game_details", "primary", "secondary", "mode_reason", "index_status", "status"):
            setattr(app, name + "_var", Variable())
        app.mode_var = Variable(MergeMode.FULL_TEXT.value)
        for name in ("primary_combo", "secondary_combo", "full_mode", "dialogue_mode",
                     "generate_button", "install_button", "modify_button", "uninstall_button",
                     "index_build_button", "operation_progress"):
            setattr(app, name, Widget())
        app._render_generation_and_install = lambda: None
        app._set_candidate_values = lambda: None
        app._set_controls_enabled = lambda _enabled: None
        return app

    def test_missing_or_stale_index_disables_dialogue_and_offers_build(self):
        game = self.make_game("index-state")
        app = self.index_app(game)
        for state in ("missing", "stale"):
            with self.subTest(state=state), patch("w3sub_app.gui.config.save_config"), \
                 patch("w3sub_app.gui.detect_configured_text_language", return_value="zh"):
                snapshot = replace(app.snapshot, dialogue_index_state=state,
                                   dialogue_index_reason="content/sample.bundle: rebuild required")
                app._apply_snapshot(snapshot)
                self.assertEqual(app.dialogue_mode.options["state"], "disabled")
                self.assertEqual(app.index_build_button.options["state"], "normal")
                self.assertIn(state.capitalize(), app.index_status_var.get())
                self.assertIn("content/sample.bundle", app.mode_reason_var.get())
                self.assertEqual((app.primary_var.get(), app.secondary_var.get()), ("zh", "en"))

    def test_index_build_runs_in_worker_and_refreshes_selected_game(self):
        import threading
        game = self.make_game("index-worker")
        app = self.index_app(game)
        caller = threading.current_thread()
        observed = []
        ready = replace(app.snapshot, dialogue_index_available=True, dialogue_index_state="ready")
        def build(scanned, progress_callback):
            observed.append((threading.current_thread(), scanned.root))
            progress_callback(2, 4)
            return object()
        def load(selected):
            observed.append((threading.current_thread(), selected.game.root))
            return ready
        applied = []
        app._apply_snapshot = applied.append
        with patch("w3sub_app.gui.build_dialogue_index", side_effect=build), \
             patch("w3sub_app.gui.scan_selected_folder", return_value=game), \
             patch.object(app, "_load_game_snapshot", side_effect=load), \
             patch("w3sub_app.gui.storefronts.refresh_candidate", return_value=self.candidate(game)):
            worker = app._build_dialogue_index()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            progress = app._messages.get_nowait()
            self.assertEqual(progress[0], "dialogue index progress")
            app._messages.put(progress)
            app._poll_messages()
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(thread is not caller for thread, _root in observed))
        self.assertEqual(applied, [ready])
        self.assertIn("ready", app.status_var.get().casefold())

    def test_index_failure_preserves_full_text_generation(self):
        game = self.make_game("index-failure")
        app = self.index_app(game)
        app.primary_var.set("en")
        app.secondary_var.set("zh")
        with patch("w3sub_app.gui.build_dialogue_index", side_effect=RuntimeError(
                "content/scripts/sample.ws: unaudited loose resource format")), \
             patch("w3sub_app.gui.scan_selected_folder", return_value=game), \
             patch("w3sub_app.gui.storefronts.refresh_candidate", return_value=self.candidate(game)), \
             patch("w3sub_app.gui.config.save_config"), \
             self.assertLogs("w3dual_subtitle", level="ERROR"):
            app._build_dialogue_index().join(timeout=2)
            app._poll_messages()
        self.assertEqual(app.snapshot.dialogue_index_state, "unavailable")
        self.assertIn("content/scripts/sample.ws", app.mode_reason_var.get())
        self.assertEqual(app.full_mode.options["state"], "normal")
        self.assertEqual(app.generate_button.options["state"], "normal")
        self.assertEqual(app.dialogue_mode.options["state"], "disabled")
        self.assertEqual(app.index_build_button.options["state"], "normal")

    def test_detected_primary_default_survives_index_refresh(self):
        for detected, expected in (("zh", ("zh", "en")), ("en", ("en", "zh"))):
            app = self.index_app(self.make_game("index-default-" + detected))
            with patch("w3sub_app.gui.config.save_config"), \
                 patch("w3sub_app.gui.detect_configured_text_language", return_value=detected):
                app._apply_snapshot(app.snapshot)
                ready = replace(app.snapshot, dialogue_index_available=True, dialogue_index_state="ready")
                app._dialogue_index_built(ready)
                self.assertEqual((app.primary_var.get(), app.secondary_var.get()), expected)
                app.primary_var.set(expected[1])
                app.secondary_var.set(expected[0])
                app._dialogue_index_built(ready)
                self.assertEqual((app.primary_var.get(), app.secondary_var.get()), expected[::-1])

    def test_old_root_index_result_does_not_replace_selected_game(self):
        app = self.index_app(self.make_game("index-old"))
        old_result = app.snapshot
        app.snapshot = self.index_app(self.make_game("index-new")).snapshot
        selected = app.snapshot
        with patch.object(app, "_apply_snapshot") as apply:
            app._dialogue_index_built(old_result)
        apply.assert_not_called()
        self.assertIs(app.snapshot, selected)

    def test_cached_index_validation_does_not_build_or_run_on_tk_thread(self):
        import threading
        game = self.make_game("index-cache")
        app = self.index_app(game)
        caller = threading.current_thread()
        observed = []
        def cached(_game):
            observed.append(threading.current_thread())
            return None
        with patch("w3sub_app.gui.generation._current_dialogue_index", side_effect=cached), \
             patch("w3sub_app.gui.build_dialogue_index") as build, \
             patch("w3sub_app.gui.install.load_install_manifest", return_value=None), \
             patch("w3sub_app.gui.generation.load_latest_generation_record", return_value=None):
            worker = start_background_operation(app._messages, "fixture cached scan",
                lambda: app._load_game_snapshot(app.snapshot.selected), lambda _value: None)
            worker.join(timeout=2)
        build.assert_not_called()
        self.assertEqual(len(observed), 1)
        self.assertIsNot(observed[0], caller)

    def test_expected_coverage_failure_reads_persisted_status_on_worker(self):
        game = self.make_game("index-coverage")
        app = self.index_app(game)
        state = self.base / "index-state"
        state.mkdir()
        reason = "content/content0/ar.w3strings: unaudited loose resource format"
        def build(_game, progress_callback):
            (state / "dialogue-index-status.json").write_text(json.dumps({
                "state": "unavailable", "diagnostic": reason,
            }), encoding="utf-8")
            return None
        with patch("w3sub_app.gui.config.state_root_for", return_value=state), \
             patch("w3sub_app.dialogue_index._state_directory", return_value=state), \
             patch("w3sub_app.gui.generation._current_dialogue_index", return_value=None), \
             patch("w3sub_app.gui.install.load_install_manifest", return_value=None), \
             patch("w3sub_app.gui.generation.load_latest_generation_record", return_value=None), \
             patch("w3sub_app.gui.build_dialogue_index", side_effect=build), \
             patch("w3sub_app.gui.scan_selected_folder", return_value=game), \
             patch("w3sub_app.gui.storefronts.refresh_candidate", return_value=self.candidate(game)), \
             patch("w3sub_app.gui.config.save_config"):
            app._build_dialogue_index().join(timeout=2)
            app._poll_messages()
        self.assertEqual(app.snapshot.dialogue_index_state, "unavailable")
        self.assertIn(reason, app.mode_reason_var.get())
        self.assertEqual(app.full_mode.options["state"], "normal")

    def test_startup_candidates_preserve_discovery_priority_then_add_saved_fallback(self):
        discovered = self.make_game("registry-game")
        saved = self.make_game("saved-game")
        bad = GameCandidate(self.base / "bad", Storefront.EPIC, "stale registry")
        calls = []

        def scanner(root, storefront, build):
            calls.append(Path(root))
            if Path(root) == bad.root:
                raise ValueError("stale candidate")
            if Path(root) == discovered.root:
                return discovered
            if Path(root) == saved.root:
                return saved
            raise ValueError("unknown candidate")

        rows = ordered_startup_games(
            [self.candidate(discovered), bad], saved.root, scanner=scanner,
        )
        self.assertEqual([row.game.root for row in rows], [discovered.root, saved.root])
        self.assertLess(calls.index(discovered.root), calls.index(saved.root))

    def test_saved_path_is_not_duplicated_when_discovery_found_same_game(self):
        game = self.make_game("one-game")
        rows = ordered_startup_games(
            [self.candidate(game)], game.root / ".", scanner=lambda *args: game,
        )
        self.assertEqual(len(rows), 1)

    def test_folder_selection_scans_again_and_languages_come_from_resources(self):
        game = self.make_game("selected", languages=("en", "zh", "pl"))
        scans = []

        def scanner(root, storefront, build):
            scans.append((Path(root), storefront, build))
            return game

        result = scan_selected_folder(game.root, scanner=scanner)
        self.assertEqual(result, game)
        self.assertEqual(scans, [(game.root, Storefront.UNKNOWN, None)])
        self.assertEqual(available_languages(game), ("en", "pl", "zh"))

    def test_default_pair_prefers_chinese_after_english_even_when_sort_order_differs(self):
        self.assertEqual(default_language_pair(("en", "fr", "pl", "zh")), ("en", "zh"))

    def test_frozen_launcher_resolves_converter_beside_packaged_executable(self):
        import sys
        from unittest.mock import patch

        executable_directory = self.base / "frozen-app"
        executable_directory.mkdir()
        converter = executable_directory / "w3strings.exe"
        converter.write_bytes(b"packaged converter")
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(executable_directory / "manager.exe")):
            self.assertEqual(default_converter_path(), converter.resolve())

    def test_stale_active_install_gates_generation_and_allows_only_safe_uninstall(self):
        actions = build_action_state(
            has_game=True, primary="en", secondary="zh", mode=MergeMode.FULL_TEXT,
            resources_available=True, converter_compatible=True,
            generated=True, generation_freshness=Freshness.CURRENT,
            install_active=True, install_comparison=Freshness.STALE,
            conflict_paths=(), dialogue_index_current=False,
        )
        self.assertFalse(actions.generate)
        self.assertFalse(actions.install)
        self.assertFalse(actions.modify)
        self.assertTrue(actions.uninstall)
        self.assertFalse(actions.dialogue_mode)

    def test_conflicts_disable_every_mutation_and_dialogue_reason_is_actionable(self):
        actions = build_action_state(
            has_game=True, primary="en", secondary="zh", mode=MergeMode.FULL_TEXT,
            resources_available=True, converter_compatible=True,
            generated=True, generation_freshness=Freshness.CURRENT,
            install_active=True, install_comparison=Freshness.CURRENT,
            conflict_paths=("content/content0/en.w3strings",),
            dialogue_index_current=False,
        )
        self.assertFalse(actions.generate)
        self.assertFalse(actions.install)
        self.assertFalse(actions.modify)
        self.assertFalse(actions.uninstall)
        self.assertIn("content/content0/en.w3strings", actions.conflict_summary)
        self.assertTrue(actions.dialogue_reason)

    def test_unavailable_converter_and_identical_pair_cannot_generate(self):
        actions = build_action_state(
            has_game=True, primary="en", secondary="en", mode=MergeMode.FULL_TEXT,
            resources_available=True, converter_compatible=False,
            generated=False, generation_freshness=None, install_active=False,
            install_comparison=None, conflict_paths=(), dialogue_index_current=False,
        )
        self.assertFalse(actions.generate)

    def test_background_operation_runs_off_calling_thread_and_queues_result(self):
        import queue
        import threading

        messages = queue.Queue()
        caller = threading.current_thread()
        worker = start_background_operation(messages, "fixture", lambda: threading.current_thread(),
                                            lambda value: value)
        worker.join(timeout=2)
        label, payload, error = messages.get(timeout=1)
        self.assertEqual(label, "fixture")
        self.assertIsNone(error)
        result, _callback = payload
        self.assertIsNot(result, caller)
        self.assertEqual(result.name, "w3sub-fixture")

    def test_submit_starts_operation_progress(self):
        class Progress:
            def __init__(self):
                self.events = []

            def start(self, *_args):
                self.events.append("start")

            def stop(self):
                self.events.append("stop")

        class Variable:
            def set(self, _value):
                pass

        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        app._busy = False
        app._progress_running = False
        app.operation_progress = Progress()
        app.status_var = Variable()
        app._set_controls_enabled = lambda _enabled: None
        app._refresh_action_buttons = lambda: None
        app._messages = object()
        app.logger = object()
        with patch("w3sub_app.gui.start_background_operation"):
            app._submit("fixture", lambda: None, lambda _result: None)

        self.assertTrue(app._busy)
        self.assertEqual(app.operation_progress.events, ["start"])

    def test_progress_stops_when_operation_completes(self):
        import queue

        class Progress:
            running = True

            def __init__(self):
                self.events = []

            def start(self, *_args):
                self.running = True
                self.events.append("start")

            def stop(self):
                self.running = False
                self.events.append("stop")

        class Variable:
            def set(self, _value):
                pass

        class Root:
            def after(self, *_args):
                pass

        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        app._busy = True
        app._progress_running = True
        app.operation_progress = Progress()
        app.status_var = Variable()
        app._messages = queue.Queue()
        app._messages.put(("fixture", (None, lambda _result: None), None))
        app._set_controls_enabled = lambda _enabled: None
        app._refresh_action_buttons = lambda: None
        app.root = Root()

        app._poll_messages()

        self.assertFalse(app._busy)
        self.assertFalse(app.operation_progress.running)
        self.assertEqual(app.operation_progress.events, ["stop"])

    def test_completion_callback_can_start_next_operation_without_stopping_progress(self):
        import queue

        class Progress:
            running = True

            def __init__(self):
                self.events = []

            def start(self, *_args):
                self.running = True
                self.events.append("start")

            def stop(self):
                self.running = False
                self.events.append("stop")

        class Variable:
            def set(self, _value):
                pass

        class Root:
            def after(self, *_args):
                pass

        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        app._busy = True
        app._progress_running = True
        app.operation_progress = Progress()
        app.status_var = Variable()
        app._messages = queue.Queue()
        app._messages.put(("first", (None, lambda _result: app._submit(
            "second", lambda: None, lambda _next: None,
        )), None))
        app._set_controls_enabled = lambda _enabled: None
        app._refresh_action_buttons = lambda: None
        app.root = Root()
        app.logger = object()

        with patch("w3sub_app.gui.start_background_operation"):
            app._poll_messages()

        self.assertTrue(app._busy)
        self.assertTrue(app.operation_progress.running)
        self.assertEqual(app.operation_progress.events, [])

    def test_window_close_is_refused_while_worker_is_active(self):
        class Variable:
            value = None

            def set(self, value):
                self.value = value

        class Root:
            destroyed = False

            def destroy(self):
                self.destroyed = True

        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        app._busy = True
        app.status_var = Variable()
        app.root = Root()
        close_handler = getattr(app, "_on_close", None)
        self.assertTrue(callable(close_handler), "the app needs a close guard")

        self.assertFalse(close_handler())
        self.assertFalse(app.root.destroyed)
        self.assertIn("wait", app.status_var.value.casefold())

        app._busy = False
        self.assertTrue(close_handler())
        self.assertTrue(app.root.destroyed)

    def test_install_review_signature_detects_lifecycle_changes(self):
        game = self.make_game("review-signature")
        converter_path = self.base / "review-converter.exe"
        converter_path.write_bytes(b"fixture converter")
        state_root = self.base / "review-state"
        generation_record = generate(
            GenerationRequest(game, "en", "zh", MergeMode.FULL_TEXT),
            state_root, FixtureConverter(converter_path),
        )
        manifest = install_generation(game, generation_record, state_root)
        signature = getattr(gui_module, "install_manifest_review_signature", None)
        self.assertTrue(callable(signature), "the GUI needs a lifecycle review signature")
        if signature is None:
            return
        require_unchanged = getattr(gui_module, "require_reviewed_manifest", None)
        self.assertTrue(callable(require_unchanged), "lifecycle actions must enforce the review signature")
        if require_unchanged is None:
            return

        captured = signature(manifest)
        self.assertIs(require_unchanged(captured, manifest), manifest)

        relative, target = next(iter(manifest.target_files.items()))
        altered_targets = (
            replace(target, relative_path=target.relative_path + ".renamed"),
            replace(target, backup_path=target.backup_path.with_name(
                "different-original.w3strings")),
            replace(target, original_sha256="0" * 64),
            replace(target, installed_sha256="f" * 64),
        )
        changed_manifests = [
            replace(manifest, generation_id="different-generation"),
            replace(manifest, primary_language="pl"),
        ]
        for altered_target in altered_targets:
            changed_targets = dict(manifest.target_files)
            changed_targets[relative] = altered_target
            changed_manifests.append(replace(manifest, target_files=changed_targets))

        for changed_manifest in changed_manifests:
            with self.subTest(manifest=changed_manifest):
                self.assertNotEqual(captured, signature(changed_manifest))
                with self.assertRaisesRegex(InstallError, "changed after confirmation"):
                    require_unchanged(captured, changed_manifest)

    def test_fresh_lifecycle_preflight_rejects_changed_manifest_before_comparison(self):
        from unittest.mock import patch

        game = self.make_game("fresh-review")
        converter_path = self.base / "fresh-review-converter.exe"
        converter_path.write_bytes(b"fixture converter")
        state_root = self.base / "fresh-review-state"
        generation_record = generate(
            GenerationRequest(game, "en", "zh", MergeMode.FULL_TEXT),
            state_root, FixtureConverter(converter_path),
        )
        manifest = install_generation(game, generation_record, state_root)
        signature = gui_module.install_manifest_review_signature(manifest)
        changed_manifest = replace(manifest, generation_id="new-generation")
        selected = gui_module.ScannedGame(
            GameCandidate(game.root, Storefront.STEAM, "fixture"), game,
        )
        app = gui_module.W3DualSubtitleApp.__new__(gui_module.W3DualSubtitleApp)
        app.snapshot = gui_module.GameSnapshot(
            selected, manifest, None, generation_record, Freshness.CURRENT, False,
        )

        with patch("w3sub_app.gui.scan_game", return_value=game), \
             patch("w3sub_app.gui.config.state_root_for", return_value=state_root), \
             patch("w3sub_app.gui.install.load_install_manifest",
                   return_value=changed_manifest), \
             patch("w3sub_app.gui.install.compare_install") as compare_install:
            with self.assertRaisesRegex(InstallError, "changed after confirmation"):
                app._fresh_game_and_manifest(reviewed_manifest_signature=signature)

        compare_install.assert_not_called()

    def test_install_confirmation_lists_pair_targets_and_cancel_starts_no_operation(self):
        confirmation = confirmation_text(
            "Install", self.base / "game", "en", "zh",
            ("content/content0/en.w3strings", "dlc/dlc0/en.w3strings"),
        )
        self.assertIn("en + zh", confirmation)
        self.assertIn("content/content0/en.w3strings", confirmation)
        self.assertIn("dlc/dlc0/en.w3strings", confirmation)
        called = []
        accepted = confirm_then_submit(lambda *_args: False, "Confirm", confirmation,
                                       lambda: called.append("started"))
        self.assertFalse(accepted)
        self.assertEqual(called, [])

    def test_uninstall_confirmation_shows_restore_paths_and_backup_directory(self):
        backup = self.base / "state" / "backups" / "install"
        text = confirmation_text(
            "Uninstall", self.base / "game", "en", "zh",
            ("content/content0/en.w3strings",), backup,
        )
        self.assertIn("Restore", text)
        self.assertIn("content/content0/en.w3strings", text)
        self.assertIn(str(backup), text)

    def test_lifecycle_completion_lists_absolute_targets_and_retained_backups(self):
        backup = self.base / "state" / "backups"
        result = completed_paths_text(
            self.base / "game", ("content/content0/en.w3strings",), backup,
        )
        self.assertIn(str(self.base / "game" / "content" / "content0" / "en.w3strings"), result)
        self.assertIn(str(backup), result)

    def test_converter_ui_summary_is_short_and_actionable_for_format_164(self):
        summary = summarize_compatibility_error(
            "content/content0/en.w3strings: converter failed\n"
            "ERROR - supported versions: 162 - 163. found: 164.\n" + "details\n" * 30
        )
        self.assertIn("format 164", summary)
        self.assertIn("162–163", summary)
        self.assertIn("compatible converter", summary.casefold())
        self.assertLess(len(summary), 360)

    def test_compatibility_round_trips_full_inventory_on_copies_and_preserves_semantics(self):
        game = self.make_game("compat", languages=("en", "zh", "pl"))
        converter_path = self.base / "converter.exe"
        converter_path.write_bytes(b"fake converter")
        converter = FixtureConverter(converter_path, mutate_input=True)
        original = {path: path.read_bytes()
                    for files in game.language_files.values() for path in files}
        sources = {
            f"content/content0/{language}.w3strings": paths[0]
            for language, paths in game.language_files.items() if language in ("en", "zh")
        }
        result = check_compatibility(sources, converter, self.base / "probe")
        self.assertTrue(result.compatible, result.error)
        self.assertEqual(result.checked_resources, 2)
        self.assertEqual(len(converter.decoded), 4)
        self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_compatibility_fails_closed_on_format_164_error_and_semantic_change(self):
        game = self.make_game("incompatible")
        converter_path = self.base / "converter.exe"
        converter_path.write_bytes(b"fake converter")
        sources = {
            f"content/content0/{language}.w3strings": paths[0]
            for language, paths in game.language_files.items()
        }
        unsupported = check_compatibility(
            sources, FixtureConverter(converter_path, fail_on="zh"), self.base / "fail-probe",
        )
        self.assertFalse(unsupported.compatible)
        self.assertIn("format 164", unsupported.error)
        changed = check_compatibility(
            sources, FixtureConverter(converter_path, corrupt_roundtrip=True),
            self.base / "semantic-probe",
        )
        self.assertFalse(changed.compatible)
        self.assertIn("semantic", changed.error.casefold())

    def test_modify_overrides_manifest_targets_even_when_their_role_changes(self):
        game = self.make_game("modify")
        converter_path = self.base / "converter.exe"
        converter_path.write_bytes(b"fixture converter")
        converter = FixtureConverter(converter_path)
        record = generate(
            GenerationRequest(game, "en", "zh", MergeMode.FULL_TEXT),
            self.base / "state", converter,
        )
        manifest = install_generation(game, record, self.base / "state")
        overrides = source_overrides_for_pair(game, manifest, "zh", "en")
        self.assertEqual(set(overrides), {"content/content0/en.w3strings"})
        self.assertEqual(Path(next(iter(overrides.values()))).read_text(encoding="utf-8"),
                         "1|00000001||en line\n")

    def test_folder_state_can_reload_latest_generation_for_that_game_only(self):
        game = self.make_game("reload")
        converter_path = self.base / "reload-converter.exe"
        converter_path.write_bytes(b"fixture converter")
        state_root = self.base / "app-state"
        record = generate(
            GenerationRequest(game, "en", "zh", MergeMode.FULL_TEXT),
            state_root, FixtureConverter(converter_path),
        )
        self.assertEqual(load_latest_generation_record(state_root, game.root), record)
        self.assertIsNone(load_latest_generation_record(state_root, self.base / "other-game"))

    def test_folder_state_ignores_generation_with_malformed_converter_hash(self):
        game = self.make_game("tampered-record")
        converter_path = self.base / "tampered-converter.exe"
        converter_path.write_bytes(b"fixture converter")
        state_root = self.base / "tampered-state"
        record = generate(
            GenerationRequest(game, "en", "zh", MergeMode.FULL_TEXT),
            state_root, FixtureConverter(converter_path),
        )
        path = record.generation_dir / "generation.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["converter_sha256"] = "bad-hash"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(load_latest_generation_record(state_root, game.root))


if __name__ == "__main__":
    unittest.main()
