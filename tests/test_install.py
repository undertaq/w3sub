import os
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from w3sub_app import install
from w3sub_app.generation import generate
from w3sub_app.models import (
    Freshness,
    GameInstallation,
    GameVersion,
    GenerationRequest,
    MergeMode,
    Storefront,
)

class FixtureConverter:
    def __init__(self, executable):
        self.executable = Path(executable)
        self.version = "fixture 1"

    def decode(self, source, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        result = work_dir / "decoded.csv"
        result.write_bytes(Path(source).read_bytes())
        return result

    def encode(self, csv_path, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        result = work_dir / "encoded.w3strings"
        result.write_bytes(Path(csv_path).read_bytes())
        return result

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="install transaction ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.game_root = self.base / "copied game"
        self.state_root = self.base / "state"
        self.converter_path = self.base / "converter.exe"
        self.converter_path.write_bytes(b"fixture converter")
        self.converter = FixtureConverter(self.converter_path)
        self.game = self.make_game()
        rescan = patch.object(install, "_rescan_game", side_effect=lambda game: game, create=True)
        rescan.start()
        self.addCleanup(rescan.stop)

    def use_native_fixture(self):
        from w3sub_app.w3strings_native import StringsFile, encode
        for language, paths in self.game.language_files.items():
            for path in paths:
                path.write_bytes(encode(StringsFile(164, 0, ((1, language),), ((7, 1),))))
        self.converter = None

    def test_install_rejects_source_changed_after_freshness_before_backup(self):
        self.use_native_fixture()
        record = self.generate_record()
        target = self.game.language_files["en"][0]
        verified = install._verified_outputs
        def changed(record, game):
            outputs = verified(record, game)
            target.write_bytes(b"updated original after freshness")
            return outputs
        with patch.object(install, "_verified_outputs", side_effect=changed):
            with self.assertRaises(install.InstallError):
                install.install_generation(self.game, record, self.state_root)
        self.assertEqual(target.read_bytes(), b"updated original after freshness")
        self.assertIsNone(install.load_install_manifest(self.state_root))

    def test_modify_rejects_new_target_changed_after_freshness_before_backup(self):
        self.use_native_fixture()
        first = self.generate_record()
        manifest = install.install_generation(self.game, first, self.state_root)
        overrides = {relative: target.backup_path for relative, target in manifest.target_files.items()}
        record = self.generate_record("zh", "en", overrides)
        new_target = self.game.language_files["zh"][0]
        old_targets = self.installed_targets(manifest)
        verified = install._verified_outputs
        def changed(record, game):
            outputs = verified(record, game)
            new_target.write_bytes(b"updated newly targeted original")
            return outputs
        with patch.object(install, "_verified_outputs", side_effect=changed):
            with self.assertRaises(install.InstallError):
                install.modify_install(self.game, record, manifest)
        self.assertEqual(new_target.read_bytes(), b"updated newly targeted original")
        self.assertEqual(self.installed_targets(manifest), old_targets)
        self.assertEqual(install.load_install_manifest(self.state_root), manifest)

    def test_install_rechecks_secondary_sources_immediately_before_mutation(self):
        record = self.generate_record()
        stage = install._sibling_stage
        secondary = self.game.language_files["zh"][0]
        def changed(target, source, prefix):
            result = stage(target, source, prefix)
            if prefix == "w3sub-stage":
                secondary.write_bytes(b"secondary changed during staging")
            return result
        originals = self.originals()
        with patch.object(install, "_sibling_stage", side_effect=changed):
            with self.assertRaises(install.InstallError):
                install.install_generation(self.game, record, self.state_root)
        self.assertEqual(self.game.language_files["en"][0].read_bytes(), originals["content/content0/en.w3strings"])
        self.assertEqual(secondary.read_bytes(), b"secondary changed during staging")

    def test_install_rechecks_version_under_lock_before_mutation(self):
        record = self.generate_record()
        updated = replace(self.game, version=replace(self.game.version, store_build_id="updated-build"))
        def rescan(game):
            self.assertTrue((Path(record.generation_dir).parents[1] / "operation.lock").is_file())
            return updated
        with patch.object(install, "_rescan_game", side_effect=rescan):
            with self.assertRaises(install.InstallError):
                install.install_generation(self.game, record, self.state_root)

    def test_install_rechecks_complete_resource_inventory_before_mutation(self):
        record = self.generate_record()
        new_en = self.game_root / "content/new/en.w3strings"
        new_zh = self.game_root / "content/new/zh.w3strings"
        new_en.parent.mkdir()
        new_en.write_bytes(b"new en")
        new_zh.write_bytes(b"new zh")
        updated = replace(self.game, language_files={"en": (*self.game.language_files["en"], new_en),
                                                   "zh": (*self.game.language_files["zh"], new_zh)})
        with patch.object(install, "_rescan_game", return_value=updated):
            with self.assertRaises(install.InstallError):
                install.install_generation(self.game, record, self.state_root)

    def test_modify_rechecks_effective_secondary_source_before_mutation(self):
        first = self.generate_record()
        manifest = install.install_generation(self.game, first, self.state_root)
        overrides = {relative: target.backup_path for relative, target in manifest.target_files.items()}
        record = self.generate_record(overrides=overrides)
        before = self.installed_targets(manifest)
        stage = install._sibling_stage
        def changed(target, source, prefix):
            staged = stage(target, source, prefix)
            if prefix == "w3sub-stage":
                self.game.language_files["zh"][0].write_bytes(b"secondary update during Modify")
            return staged
        with patch.object(install, "_sibling_stage", side_effect=changed):
            with self.assertRaises(install.InstallError):
                install.modify_install(self.game, record, manifest)
        self.assertEqual(self.installed_targets(manifest), before)
        self.assertEqual(install.load_install_manifest(self.state_root), manifest)
        self.assertEqual(self.game.language_files["zh"][0].read_bytes(), b"secondary update during Modify")

    def test_rollback_preserves_third_party_bytes_and_recovery_state(self):
        record = self.generate_record()
        first = self.game_root / "content/content0/en.w3strings"
        second = self.game_root / "content/dlc0/en.w3strings"
        original = first.read_bytes()
        real_replace = os.replace
        def interfere(source, destination):
            destination = Path(destination)
            if destination == second and "w3sub-stage" in Path(source).name:
                raise PermissionError("second replacement failed")
            result = real_replace(source, destination)
            if destination == first and "w3sub-stage" in Path(source).name:
                first.write_bytes(b"third-party bytes after replacement")
            return result
        with patch.object(install.os, "replace", side_effect=interfere):
            with self.assertRaises(install.InstallError) as caught:
                install.install_generation(self.game, record, self.state_root)
        self.assertEqual(first.read_bytes(), b"third-party bytes after replacement")
        self.assertIn("content/content0/en.w3strings", " ".join(caught.exception.rollback_errors))
        manifest = install.load_install_manifest(self.state_root)
        self.assertTrue(manifest.prepared and manifest.conflicted)
        self.assertEqual(manifest.conflict_paths, ("content/content0/en.w3strings",))
        self.assertEqual(manifest.target_files["content/content0/en.w3strings"].backup_path.read_bytes(), original)
        snapshots = list(manifest.state_directory.glob("transactions/*/content/content0/en.w3strings"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_bytes(), original)

    def assert_provenance_persisted(self, record):
        manifest = install.install_generation(self.game, record, self.state_root)
        payload = json.loads((manifest.state_directory / "install.json").read_text(encoding="utf-8"))
        self.assertIn("generation_provenance", payload)
        expected = {"codec_kind": record.codec_kind, "converter_path": record.converter_path,
                    "converter_sha256": record.converter_sha256, "converter_version": record.converter_version,
                    "app_version": record.app_version, "classifier_schema_version": record.classifier_schema_version}
        self.assertEqual(payload["generation_provenance"], expected)
        for path in Path(record.generation_dir).rglob("*"):
            if path.is_file():
                path.unlink()
        loaded = install.load_install_manifest(self.state_root)
        self.assertEqual(loaded, manifest)
        payload.pop("generation_provenance")
        (manifest.state_directory / "install.json").write_text(json.dumps(payload), encoding="utf-8")
        legacy = install.load_install_manifest(self.state_root)
        self.assertIsNone(legacy.generation_provenance)
        self.assertTrue(legacy.active)

    def test_native_manifest_retains_generation_provenance_without_staged_generation(self):
        self.use_native_fixture()
        self.assert_provenance_persisted(self.generate_record())

    def test_external_manifest_retains_generation_provenance_without_staged_generation(self):
        self.assert_provenance_persisted(self.generate_record())

    def test_malformed_generation_provenance_cannot_load_as_valid_install(self):
        manifest = install.install_generation(self.game, self.generate_record(), self.state_root)
        path = manifest.state_directory / "install.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for field, value in (("codec_kind", "unknown"), ("converter_sha256", "not a hash"),
                             ("classifier_schema_version", True), ("converter_path", "relative.exe")):
            payload = json.loads(json.dumps(original))
            payload["generation_provenance"][field] = value
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(install.InstallError):
                install.load_install_manifest(self.state_root)

    def make_game(self):
        languages = {"en": [], "zh": []}
        for directory in ("content/content0", "content/dlc0"):
            for language, text in (("en", "English"), ("zh", "Chinese")):
                path = self.game_root / directory / f"{language}.w3strings"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"1|00000001||{text} {directory}\n", encoding="utf-8")
                languages[language].append(path)
        return GameInstallation(
            self.game_root,
            Storefront.STEAM,
            GameVersion("5.0.0.1044392", "build-a", "5.0.0.1044392(Build Machine)"),
            {language: tuple(paths) for language, paths in languages.items()},
        )

    def originals(self):
        return {
            path.relative_to(self.game_root).as_posix(): path.read_bytes()
            for paths in self.game.language_files.values()
            for path in paths
        }

    def generate_record(self, primary="en", secondary="zh", overrides=None, game=None):
        return generate(
            GenerationRequest(game or self.game, primary, secondary,
                              MergeMode.FULL_TEXT, overrides),
            self.state_root,
            self.converter,
        )

    def installed_targets(self, manifest):
        return {
            path: (self.game_root / Path(*Path(path).parts)).read_bytes()
            for path in manifest.target_files
        }

    def test_install_modify_changed_pair_and_uninstall_restore_exact_originals(self):
        originals = self.originals()
        first = self.generate_record()
        manifest = install.install_generation(self.game, first, self.state_root)
        self.assertTrue(manifest.active)
        self.assertEqual(set(manifest.target_files), {
            "content/content0/en.w3strings", "content/dlc0/en.w3strings"
        })
        self.assertEqual(install.load_install_manifest(self.state_root), manifest)
        self.assertNotEqual(self.installed_targets(manifest), {
            path: originals[path] for path in manifest.target_files
        })

        overrides = {
            relative: entry.backup_path
            for relative, entry in manifest.target_files.items()
            if relative in {
                "content/content0/en.w3strings", "content/dlc0/en.w3strings"
            }
        }
        changed_pair = self.generate_record("zh", "en", overrides)
        updated = install.modify_install(self.game, changed_pair, manifest)
        self.assertTrue(updated.active)
        self.assertEqual(set(updated.target_files), {
            "content/content0/zh.w3strings", "content/dlc0/zh.w3strings"
        })
        for relative in ("content/content0/en.w3strings", "content/dlc0/en.w3strings"):
            self.assertEqual((self.game_root / relative).read_bytes(), originals[relative])
        self.assertEqual(
            install.compare_install(updated, self.game).conflict_paths, ()
        )

        result = install.uninstall(self.game, updated)
        self.assertEqual(set(result.restored_paths), set(updated.target_files))
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.rollback_errors, ())
        self.assertTrue(Path(result.backup_directory).is_dir())
        self.assertEqual(self.originals(), originals)
        self.assertFalse(install.load_install_manifest(self.state_root).active)

    def test_install_rejects_generation_when_inputs_changed_or_are_unreadable(self):
        record = self.generate_record()
        self.game.language_files["zh"][0].write_text("source changed\n", encoding="utf-8")
        with self.assertRaises(install.InstallError):
            install.install_generation(self.game, record, self.state_root)
        self.assertFalse((self.game_root / "content/content0/en.w3strings").read_bytes()
                         == Path(record.output_files["content/content0/en.w3strings"]).read_bytes())

    def test_partial_install_failure_rolls_back_every_replaced_target(self):
        originals = self.originals()
        record = self.generate_record()
        failing_target = (self.game_root / "content/dlc0/en.w3strings").resolve()
        real_replace = os.replace

        def fail_second(source, destination):
            if Path(destination).resolve() == failing_target:
                raise PermissionError("simulated locked target")
            return real_replace(source, destination)

        with patch.object(install.os, "replace", side_effect=fail_second):
            with self.assertRaises(install.InstallError) as caught:
                install.install_generation(self.game, record, self.state_root)
        self.assertEqual(caught.exception.rollback_errors, ())
        self.assertEqual(self.originals(), originals)
        self.assertFalse(list(self.state_root.glob("games/*/operation.lock")))

    def test_partial_install_failure_reports_rollback_failure_and_exact_path(self):
        record = self.generate_record()
        first_target = (self.game_root / "content/content0/en.w3strings").resolve()
        second_target = (self.game_root / "content/dlc0/en.w3strings").resolve()
        real_replace = os.replace

        def fail_replace(source, destination):
            destination = Path(destination).resolve()
            if destination == second_target:
                raise PermissionError("simulated second target lock")
            if destination == first_target and "rollback" in Path(source).name:
                raise PermissionError("simulated rollback lock")
            return real_replace(source, destination)

        with patch.object(install.os, "replace", side_effect=fail_replace):
            with self.assertRaises(install.InstallError) as caught:
                install.install_generation(self.game, record, self.state_root)
        self.assertTrue(caught.exception.rollback_errors)
        self.assertIn("content/content0/en.w3strings",
                      " ".join(caught.exception.rollback_errors))
        prepared = install.load_install_manifest(self.state_root)
        self.assertTrue(prepared.conflicted)
        self.assertEqual(prepared.conflict_paths, ("content/content0/en.w3strings",))

    def test_uninstall_result_surfaces_rollback_failure_and_backup_location(self):
        record = self.generate_record()
        manifest = install.install_generation(self.game, record, self.state_root)
        first_target = (self.game_root / "content/content0/en.w3strings").resolve()
        second_target = (self.game_root / "content/dlc0/en.w3strings").resolve()
        real_replace = os.replace

        def fail_restore(source, destination):
            destination = Path(destination).resolve()
            if destination == second_target:
                raise PermissionError("simulated restore lock")
            if destination == first_target and "w3sub-rollback" in Path(source).name:
                raise PermissionError("simulated rollback lock")
            return real_replace(source, destination)

        with patch.object(install.os, "replace", side_effect=fail_restore):
            result = install.uninstall(self.game, manifest)
        self.assertTrue(result.rollback_errors)
        self.assertIn("content/content0/en.w3strings",
                      " ".join(result.rollback_errors))
        self.assertEqual(result.backup_directory, manifest.backup_directory)

    def test_external_edit_is_reported_and_uninstall_preserves_it(self):
        record = self.generate_record()
        manifest = install.install_generation(self.game, record, self.state_root)
        changed_path = "content/dlc0/en.w3strings"
        changed_file = self.game_root / changed_path
        changed_file.write_bytes(b"third-party edit")

        comparison = install.compare_install(manifest, self.game)
        self.assertEqual(comparison.conflict_paths, (changed_path,))
        result = install.uninstall(self.game, manifest)
        self.assertEqual(result.conflicts, (changed_path,))
        self.assertEqual(changed_file.read_bytes(), b"third-party edit")
        self.assertEqual((self.game_root / "content/content0/en.w3strings").read_bytes(),
                         Path(record.output_files["content/content0/en.w3strings"]).read_bytes())

    def test_source_update_and_target_conflict_are_reported_separately(self):
        record = self.generate_record()
        manifest = install.install_generation(self.game, record, self.state_root)
        self.game.language_files["zh"][0].write_bytes(b"updated game language")
        game_update = GameInstallation(
            self.game.root, self.game.storefront,
            GameVersion("5.0.0.1044393", "build-b", "5.0.0.1044393(Build Machine)"),
            self.game.language_files,
        )
        comparison = install.compare_install(manifest, game_update)
        self.assertEqual(comparison.freshness, Freshness.STALE)
        self.assertEqual(comparison.conflict_paths, ())

        target = self.game_root / "content/content0/en.w3strings"
        target.write_bytes(b"game updater replaced managed target")
        comparison = install.compare_install(manifest, game_update)
        self.assertEqual(comparison.freshness, Freshness.STALE)
        self.assertEqual(comparison.conflict_paths, ("content/content0/en.w3strings",))

    def test_missing_original_backup_makes_uninstall_unsafe_for_that_target(self):
        record = self.generate_record()
        manifest = install.install_generation(self.game, record, self.state_root)
        relative = "content/dlc0/en.w3strings"
        target = self.game_root / relative
        installed = target.read_bytes()
        manifest.target_files[relative].backup_path.unlink()

        comparison = install.compare_install(manifest, self.game)
        self.assertEqual(comparison.conflict_paths, (relative,))
        self.assertFalse(comparison.uninstall_safe)
        result = install.uninstall(self.game, manifest)
        self.assertEqual(result.conflicts, (relative,))
        self.assertEqual(target.read_bytes(), installed)

    def test_copy_new_preserves_a_destination_it_did_not_create(self):
        source = self.base / "copy source.bin"
        destination = self.base / "existing destination.bin"
        source.write_bytes(b"replacement")
        destination.write_bytes(b"preserve existing")

        with self.assertRaises(FileExistsError):
            install._copy_new(source, destination)

        self.assertEqual(destination.read_bytes(), b"preserve existing")

    def test_new_backup_rejects_redirected_parent_without_touching_outside_or_game(self):
        first_generation = self.generate_record()
        manifest = install.install_generation(self.game, first_generation, self.state_root)
        new_directory = self.game_root / "content/new0"
        new_directory.mkdir()
        new_en = new_directory / "en.w3strings"
        new_en.write_bytes(b"new English baseline")

        outside_directory = self.base / "outside backup destination"
        outside_directory.mkdir()
        outside_target = outside_directory / "en.w3strings"
        outside_target.write_bytes(new_en.read_bytes())
        redirected_parent = manifest.backup_directory / "content" / "new0"
        try:
            redirected_parent.symlink_to(outside_directory, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation is not available")

        game_before = {
            path.relative_to(self.game_root).as_posix(): path.read_bytes()
            for path in self.game_root.rglob("*") if path.is_file()
        }
        outside_before = outside_target.read_bytes()
        with self.assertRaises(install.InstallError):
            install._copy_new_backups(
                manifest, self.game, {"content/new0/en.w3strings"}
            )

        game_after = {
            path.relative_to(self.game_root).as_posix(): path.read_bytes()
            for path in self.game_root.rglob("*") if path.is_file()
        }
        self.assertEqual(game_after, game_before)
        self.assertEqual(outside_target.read_bytes(), outside_before)
        self.assertEqual(tuple(outside_directory.iterdir()), (outside_target,))

    def test_unexpected_files_survive_uninstall(self):
        record = self.generate_record()
        manifest = install.install_generation(self.game, record, self.state_root)
        unexpected = self.game_root / "content/content0/third-party.txt"
        unexpected.write_bytes(b"keep me")
        install.uninstall(self.game, manifest)
        self.assertEqual(unexpected.read_bytes(), b"keep me")

    def test_operation_lock_is_exclusive_and_left_for_explicit_stale_recovery(self):
        record = self.generate_record()
        state_game_dir = Path(record.generation_dir).parents[1]
        lock = state_game_dir / "operation.lock"
        lock.write_text("pid=fixture\n", encoding="utf-8")
        with self.assertRaisesRegex(install.InstallError, "lock"):
            install.install_generation(self.game, record, self.state_root)
        self.assertEqual(lock.read_text(encoding="utf-8"), "pid=fixture\n")

    def test_lifecycle_preflight_rejects_a_running_game_process(self):
        originals = self.originals()
        record = self.generate_record()
        with patch.object(install, "_running_game_processes",
                          return_value=("witcher3.exe",), create=True):
            with self.assertRaisesRegex(install.InstallError, "running"):
                install.install_generation(self.game, record, self.state_root)
        self.assertEqual(self.originals(), originals)

    def test_metadata_only_game_version_change_can_install(self):
        record = self.generate_record()
        updated = GameInstallation(
            self.game.root, self.game.storefront,
            GameVersion("5.0.0.1044393", "build-b", "5.0.0.1044393(Build Machine)"),
            self.game.language_files,
        )
        manifest = install.install_generation(updated, record, self.state_root)
        self.assertEqual(manifest.generation_version, self.game.version)
        self.assertEqual(manifest.install_version, updated.version)
        self.assertEqual(install.compare_install(manifest, self.game).freshness,
                         Freshness.VERSION_METADATA_CHANGED_ONLY)

    def test_target_symlink_escape_is_rejected_before_any_replacement(self):
        record = self.generate_record()
        outside = self.base / "outside.w3strings"
        outside.write_bytes(b"preserve outside")
        target = self.game_root / "content/dlc0/en.w3strings"
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available")
        before = (self.game_root / "content/content0/en.w3strings").read_bytes()
        with self.assertRaises(install.InstallError):
            install.install_generation(self.game, record, self.state_root)
        self.assertEqual(outside.read_bytes(), b"preserve outside")
        self.assertEqual((self.game_root / "content/content0/en.w3strings").read_bytes(), before)

if __name__ == "__main__":
    unittest.main()
