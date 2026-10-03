import os
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
