"""End-to-end acceptance checks against a copied, temporary game fixture."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from w3sub_app import install
from w3sub_app.game import scan_game
from w3sub_app.generation import compare_generation, generate
from w3sub_app.models import (
    Freshness,
    GenerationRequest,
    MergeMode,
    Storefront,
)


class FixtureConverter:
    """Deterministic stand-in for the external converter executable."""

    def __init__(self, executable):
        self.executable = Path(executable)
        self.version = "workflow-fixture-converter 1"
        self.decoded_sources = []
        self.sequence = 0

    def decode(self, source, work_dir):
        source = Path(source)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        self.sequence += 1
        self.decoded_sources.append(source.read_bytes())
        output = work_dir / f"decoded-{self.sequence}.csv"
        output.write_bytes(source.read_bytes())
        return output

    def encode(self, csv_path, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        self.sequence += 1
        output = work_dir / f"encoded-{self.sequence}.w3strings"
        output.write_bytes(Path(csv_path).read_bytes())
        return output


class WorkflowTests(unittest.TestCase):
    def test_native_binary_lifecycle_restores_exact_originals_in_copied_fixture(self):
        from w3sub_app.w3strings_native import StringsFile, encode, decode
        for path in self.game_root.rglob("*.w3strings"):
            path.write_bytes(encode(StringsFile(164, 0, ((1, path.stem + "\r\ntext\n\r"),), ((7, 1), (8, 1)))))
        game = self._scan()
        originals = self._original_bytes()
        record = generate(self._request(game), self.state_root)
        self.assertEqual(record.codec_kind, "native")
        with patch.object(install, "_running_game_processes", return_value=()):
            manifest = install.install_generation(game, record, self.state_root)
            for relative in manifest.target_files:
                self.assertEqual(decode((game.root / relative).read_bytes()).strings,
                                 ((1, "en\r\ntext\n\r<br>zh\r\ntext\n\r"),))
            overrides = {relative: target.backup_path for relative, target in manifest.target_files.items()}
            changed_pair = generate(self._request(game, "zh", "en", overrides), self.state_root)
            modified = install.modify_install(game, changed_pair, manifest)
            for relative in modified.target_files:
                self.assertEqual(decode((game.root / relative).read_bytes()).strings,
                                 ((1, "zh\r\ntext\n\r<br>en\r\ntext\n\r"),))
            restored = install.uninstall(game, modified)
        self.assertEqual(restored.conflicts, ())
        self.assertEqual(self._original_bytes(), originals)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow acceptance ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.template_root = self.base / "fixture source"
        self.game_root = self.base / "copied game"
        self.state_root = self.base / "app state"
        self.converter_path = self.base / "fixture converter.exe"
        self.converter_path.write_bytes(b"deterministic converter fixture")
        self.converter = FixtureConverter(self.converter_path)
        self.executable_version = "5.0.0.1044392 (Build Machine)"
        self._make_template_game()
        # Lifecycle operations always target this copied tree inside TemporaryDirectory.
        shutil.copytree(self.template_root, self.game_root)

    def _make_template_game(self):
        executable = self.template_root / "bin" / "x64_dx12" / "witcher3.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fixture executable; version is injected")
        for relative_dir in ("content/content0", "content/dlc0"):
            directory = self.template_root / relative_dir
            directory.mkdir(parents=True)
            for language, text in (
                ("en", "English"),
                ("zh", "Chinese"),
                ("fr", "French"),
            ):
                resource = directory / f"{language}.w3strings"
                resource.write_text(
                    f"1|00000001||{text} {relative_dir}\n", encoding="utf-8"
                )
            (directory / "unrelated.bin").write_bytes(b"leave this file alone")

    def _read_version(self, executable):
        self.assertEqual(Path(executable).name.casefold(), "witcher3.exe")
        return self.executable_version

    def _scan(self):
        return scan_game(
            self.game_root,
            Storefront.STEAM,
            "fixture-build-1",
            version_reader=self._read_version,
        )

    def _request(self, game, primary="en", secondary="zh", overrides=None):
        return GenerationRequest(
            game=game,
            primary_language=primary,
            secondary_language=secondary,
            mode=MergeMode.FULL_TEXT,
            source_overrides=overrides,
        )

    def _original_bytes(self):
        return {
            path.relative_to(self.game_root).as_posix(): path.read_bytes()
            for path in sorted(self.game_root.rglob("*.w3strings"))
        }

    def _unrelated_bytes(self):
        return {
            path.relative_to(self.game_root).as_posix(): path.read_bytes()
            for path in sorted(self.game_root.rglob("unrelated.bin"))
        }

    def test_scan_tracks_available_languages_and_source_freshness(self):
        game = self._scan()
        self.assertEqual(game.version.executable_version, "5.0.0.1044392")
        self.assertEqual(game.version.store_build_id, "fixture-build-1")
        self.assertEqual(set(game.language_files), {"en", "fr", "zh"})
        self.assertEqual(len(game.language_files["en"]), 2)

        record = generate(self._request(game), self.state_root, self.converter)
        self.assertEqual(record.game_version, game.version)
        self.assertEqual((record.primary_language, record.secondary_language), ("en", "zh"))
        self.assertEqual(compare_generation(record, game), Freshness.CURRENT)

        self.executable_version = "5.0.0.1044393 (Build Machine)"
        metadata_only_update = self._scan()
        self.assertEqual(
            compare_generation(record, metadata_only_update),
            Freshness.VERSION_METADATA_CHANGED_ONLY,
        )

        changed_resource = self.game_root / "content" / "content0" / "zh.w3strings"
        changed_resource.write_text("1|00000001||Updated Chinese\n", encoding="utf-8")
        source_update = self._scan()
        self.assertEqual(compare_generation(record, source_update), Freshness.STALE)

    def test_install_modify_from_originals_and_uninstall_restore_exact_bytes(self):
        game = self._scan()
        originals = self._original_bytes()
        unrelated = self._unrelated_bytes()
        first = generate(self._request(game), self.state_root, self.converter)

        with patch.object(install, "_running_game_processes", return_value=()):
            manifest = install.install_generation(game, first, self.state_root)
            self.assertTrue(manifest.active)
            self.assertEqual(
                set(manifest.target_files),
                {"content/content0/en.w3strings", "content/dlc0/en.w3strings"},
            )
            installed = {
                relative: (self.game_root / Path(*relative.split("/"))).read_text(
                    encoding="utf-8"
                )
                for relative in manifest.target_files
            }
            self.assertIn("English content/content0<br>Chinese content/content0",
                          installed["content/content0/en.w3strings"])
            self.assertIn("English content/dlc0<br>Chinese content/dlc0",
                          installed["content/dlc0/en.w3strings"])
            self.assertEqual(self._unrelated_bytes(), unrelated)

            # Change the pair so English is now secondary. Its source must come
            # from the first install's exact backup, not from merged game bytes.
            overrides = {
                relative: target.backup_path
                for relative, target in manifest.target_files.items()
            }
            decoded_before_modify = len(self.converter.decoded_sources)
            changed_pair = generate(
                self._request(game, "zh", "en", overrides),
                self.state_root,
                self.converter,
            )
            self.assertEqual(
                compare_generation(changed_pair, game, overrides), Freshness.CURRENT
            )
            original_english = [
                payload for relative, payload in originals.items()
                if Path(relative).name == "en.w3strings"
            ]
            decoded_for_modify = self.converter.decoded_sources[decoded_before_modify:]
            for payload in original_english:
                self.assertIn(payload, decoded_for_modify)

            modified = install.modify_install(game, changed_pair, manifest)
            self.assertTrue(modified.active)
            self.assertEqual(
                set(modified.target_files),
                {"content/content0/zh.w3strings", "content/dlc0/zh.w3strings"},
            )
            for relative in ("content/content0/en.w3strings", "content/dlc0/en.w3strings"):
                self.assertEqual((self.game_root / relative).read_bytes(), originals[relative])
            for relative in modified.target_files:
                text = (self.game_root / Path(*relative.split("/"))).read_text(encoding="utf-8")
                self.assertIn("Chinese content/", text)
                self.assertIn("<br>English content/", text)

            result = install.uninstall(game, modified)

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.rollback_errors, ())
        self.assertEqual(self._original_bytes(), originals)
        self.assertEqual(self._unrelated_bytes(), unrelated)

    def test_uninstall_preserves_external_conflict_without_overwriting_it(self):
        game = self._scan()
        originals = self._original_bytes()
        record = generate(self._request(game), self.state_root, self.converter)
        changed_relative = "content/content0/en.w3strings"

        with patch.object(install, "_running_game_processes", return_value=()):
            manifest = install.install_generation(game, record, self.state_root)
            external_edit = b"third-party edit that must survive\n"
            (self.game_root / changed_relative).write_bytes(external_edit)
            result = install.uninstall(game, manifest)

        self.assertEqual(result.conflicts, (changed_relative,))
        self.assertEqual((self.game_root / changed_relative).read_bytes(), external_edit)
        self.assertNotEqual(self._original_bytes()[changed_relative], originals[changed_relative])

    def test_running_game_preflight_rejects_install_before_game_files_change(self):
        game = self._scan()
        originals = self._original_bytes()
        record = generate(self._request(game), self.state_root, self.converter)

        with patch.object(install, "_running_game_processes", return_value=("witcher3.exe",)):
            with self.assertRaisesRegex(install.InstallError, "process is running"):
                install.install_generation(game, record, self.state_root)

        self.assertEqual(self._original_bytes(), originals)


if __name__ == "__main__":
    unittest.main()
