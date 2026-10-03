import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from w3sub_app import generation
from w3sub_app.dialogue_index import DialogContext
from w3sub_app.generation import GenerationError, compare_generation, generate
from w3sub_app.models import (
    Freshness,
    GameInstallation,
    GameVersion,
    GenerationRequest,
    MergeMode,
    Storefront,
)


class FakeConverter:
    def __init__(self, executable):
        self.executable = Path(executable)
        self.version = "fixture-converter 1.2"
        self.decoded_sources = []
        self._next = 0

    def decode(self, source, work_dir):
        source = Path(source)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        self._next += 1
        self.decoded_sources.append(source.read_bytes())
        output = work_dir / f"decoded-{self._next}.csv"
        output.write_bytes(source.read_bytes())
        return output

    def encode(self, csv_path, work_dir):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        self._next += 1
        output = Path(work_dir) / f"encoded-{self._next}.w3strings"
        output.write_bytes(Path(csv_path).read_bytes())
        return output


class FakeDialogueIndex:
    validated = True

    def __init__(self, digest="classifier-a", current=True):
        self.digest = digest
        self.current = current

    def is_current(self, game):
        return self.current

    def context_for(self, string_id, key):
        return DialogContext.SCENE_SUBTITLE


class ExpiringDialogueIndex(FakeDialogueIndex):
    def __init__(self):
        super().__init__()
        self.checks = 0

    def is_current(self, game):
        self.checks += 1
        return self.checks == 1


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="generation ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.game_root = self.base / "game"
        self.state_root = self.base / "state"
        self.executable = self.base / "converter.exe"
        self.executable.write_bytes(b"fake converter executable")
        self.converter = FakeConverter(self.executable)
        self.game = self.make_game()

    def write_language(self, language, relative_dir="content/content0", text=None):
        path = self.game_root / relative_dir / f"{language}.w3strings"
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = f"1|00000001||{language} text\n"
        path.write_text(text, encoding="utf-8")
        return path

    def make_game(self, version="5.0.0.1044392", build="build-1", storefront=Storefront.STEAM):
        primary = self.write_language("en")
        secondary = self.write_language("zh")
        return GameInstallation(
            root=self.game_root,
            storefront=storefront,
            version=GameVersion(version, build, f"{version}(Build Machine)"),
            language_files={"en": (primary,), "zh": (secondary,)},
        )

    def request(self, game=None, mode=MergeMode.FULL_TEXT, overrides=None):
        return GenerationRequest(
            game=game or self.game,
            primary_language="en",
            secondary_language="zh",
            mode=mode,
            source_overrides=overrides,
        )

    def test_generation_stages_paired_outputs_and_persists_auditable_metadata(self):
        record = generate(self.request(), self.state_root, self.converter)

        self.assertEqual(set(record.output_files), {"content/content0/en.w3strings"})
        output = Path(record.output_files["content/content0/en.w3strings"])
        self.assertTrue(output.is_file())
        self.assertEqual(output.read_text(encoding="utf-8"),
                         "1|00000001||en text<br>zh text\n")
        self.assertFalse(output.is_relative_to(self.game_root))
        self.assertEqual(record.game_version, self.game.version)
        self.assertEqual(record.storefront, Storefront.STEAM)
        self.assertEqual(set(record.source_fingerprint.entries), {
            "content/content0/en.w3strings", "content/content0/zh.w3strings"
        })
        metadata_path = output.parents[2] / "generation.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["game_version"]["executable_version_raw"],
                         "5.0.0.1044392(Build Machine)")
        self.assertEqual(metadata["game_version"]["store_build_id"], "build-1")
        self.assertEqual(metadata["converter_path"], str(self.executable.resolve()))
        self.assertEqual(metadata["converter_sha256"],
                         hashlib.sha256(self.executable.read_bytes()).hexdigest())
        self.assertEqual(metadata["converter_version"], "fixture-converter 1.2")
        self.assertTrue(metadata["app_version"])
        self.assertEqual(metadata["output_hashes"], record.output_hashes)
        self.assertEqual(metadata["source_fingerprint"]["entries"],
                         record.source_fingerprint.entries)

    def test_identical_inputs_with_only_game_version_or_store_build_change_are_metadata_only(self):
        record = generate(self.request(), self.state_root, self.converter)
        updated = GameInstallation(
            self.game.root,
            self.game.storefront,
            GameVersion("5.0.0.1044393", "build-2", "5.0.0.1044393(Build Machine)"),
            self.game.language_files,
        )

        self.assertEqual(compare_generation(record, updated),
                         Freshness.VERSION_METADATA_CHANGED_ONLY)

    def test_changed_added_and_removed_language_sources_are_stale(self):
        record = generate(self.request(), self.state_root, self.converter)
        primary = self.game.language_files["en"][0]
        primary.write_text("1|00000001||updated\n", encoding="utf-8")
        self.assertEqual(compare_generation(record, self.game), Freshness.STALE)

        record = generate(self.request(), self.state_root, self.converter)
        added = self.write_language("en", "content/dlc0")
        added_secondary = self.write_language("zh", "content/dlc0")
        game_with_added = GameInstallation(
            self.game.root, self.game.storefront, self.game.version,
            {
                **self.game.language_files,
                "en": (*self.game.language_files["en"], added),
                "zh": (*self.game.language_files["zh"], added_secondary),
            },
        )
        self.assertEqual(compare_generation(record, game_with_added), Freshness.STALE)

        record = generate(self.request(game=game_with_added), self.state_root, self.converter)
        added.unlink()
        added_secondary.unlink()
        game_without_added = GameInstallation(
            self.game.root, self.game.storefront, self.game.version, self.game.language_files,
        )
        self.assertEqual(compare_generation(record, game_without_added), Freshness.STALE)

    def test_unreadable_language_inventory_is_reported_separately(self):
        record = generate(self.request(), self.state_root, self.converter)
        with patch.object(generation, "_fingerprint_sources",
                          side_effect=PermissionError("denied")):
            self.assertEqual(compare_generation(record, self.game), Freshness.UNREADABLE)

    def test_dialogue_generation_requires_a_current_validated_index_and_tracks_digest(self):
        with patch.object(generation, "load_dialogue_index", return_value=None):
            with self.assertRaises(GenerationError):
                generate(self.request(mode=MergeMode.DIALOGUE_ONLY),
                         self.state_root, self.converter)

        first_index = FakeDialogueIndex("classifier-a")
        with patch.object(generation, "load_dialogue_index", return_value=first_index):
            record = generate(self.request(mode=MergeMode.DIALOGUE_ONLY),
                              self.state_root, self.converter)
        self.assertEqual(record.classifier_digest, "classifier-a")

        with patch.object(generation, "load_dialogue_index",
                          return_value=FakeDialogueIndex("classifier-b")):
            self.assertEqual(compare_generation(record, self.game), Freshness.STALE)

    def test_dialogue_index_that_expires_during_merge_cannot_publish_generation(self):
        index = ExpiringDialogueIndex()
        with patch.object(generation, "load_dialogue_index", return_value=index):
            with self.assertRaisesRegex(GenerationError, "index changed during generation"):
                generate(self.request(mode=MergeMode.DIALOGUE_ONLY),
                         self.state_root, self.converter)

    def test_modify_overrides_are_used_and_fingerprinted_by_game_relative_path(self):
        primary_target = "content/content0/en.w3strings"
        original = self.base / "backup" / "en.w3strings"
        original.parent.mkdir()
        original.write_text("1|00000001||original baseline\n", encoding="utf-8")
        overrides = {primary_target: original}
        record = generate(self.request(overrides=overrides), self.state_root, self.converter)

        self.assertIn(original.read_bytes(), self.converter.decoded_sources)
        self.assertEqual(record.source_fingerprint.entries[primary_target],
                         hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(compare_generation(record, self.game, overrides), Freshness.CURRENT)
        self.assertEqual(compare_generation(record, self.game), Freshness.STALE)

    def test_game_or_source_update_after_generation_is_detected_before_install(self):
        record = generate(self.request(), self.state_root, self.converter)
        same_assets_new_build = GameInstallation(
            self.game.root,
            self.game.storefront,
            GameVersion("5.0.0.1044393", "build-2", "5.0.0.1044393(Build Machine)"),
            self.game.language_files,
        )
        self.assertEqual(compare_generation(record, same_assets_new_build),
                         Freshness.VERSION_METADATA_CHANGED_ONLY)

        self.game.language_files["zh"][0].write_text(
            "1|00000001||changed after generation\n", encoding="utf-8")
        self.assertEqual(compare_generation(record, self.game), Freshness.STALE)


if __name__ == "__main__":
    unittest.main()
