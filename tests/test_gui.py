from pathlib import Path
import json
import tempfile
import unittest

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
from w3sub_app.install import install_generation
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
