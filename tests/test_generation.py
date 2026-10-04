import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from w3sub_app import generation
from w3sub_app.dialogue_index import INDEX_SCHEMA_VERSION, DialogContext, DialogueIndex
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
    schema_version = INDEX_SCHEMA_VERSION

    def __init__(self, digest="classifier-a", current=True):
        self.digest = digest
        self.current = current

    def is_current(self, game):
        return self.current

    def context_for(self, string_id, key):
        return DialogContext.SCENE_SUBTITLE

    def context_for_id(self, string_id):
        return DialogContext.SCENE_SUBTITLE


class ExpiringDialogueIndex(FakeDialogueIndex):
    def __init__(self):
        super().__init__()
        self.checks = 0

    def is_current(self, game):
        self.checks += 1
        return self.checks == 1


class GenerationTests(unittest.TestCase):
    def fixture_index(self):
        source = self.game_root / "refs" / "scene.json"
        source.parent.mkdir(parents=True)
        source.write_text("controlled reference inventory", encoding="utf-8")
        return DialogueIndex.from_validated_references(
            self.game, [source], {("1", "0"): [DialogContext.SCENE_SUBTITLE]},
            source_roots=[source.parent], source_patterns=["scene*.json"])

    def test_native_keyless_dialogue_generation_records_index_schema_and_digest(self):
        from w3sub_app.w3strings_native import StringsFile, decode, encode
        for language in ("en", "zh"):
            for path in self.game.language_files[language]:
                path.write_bytes(encode(StringsFile(164, 0, ((1, language),), ())))
        index = self.fixture_index()
        with patch.object(generation, "load_dialogue_index", return_value=index):
            record = generate(self.request(mode=MergeMode.DIALOGUE_ONLY), self.state_root)
            self.assertEqual(compare_generation(record, self.game), Freshness.CURRENT)
            self.assertEqual(compare_generation(replace(record, classifier_schema_version=1), self.game),
                             Freshness.STALE)
        output = Path(record.output_files["content/content0/en.w3strings"])
        self.assertEqual(decode(output.read_bytes()).strings, ((1, "en<br>zh"),))
        self.assertEqual(decode(output.read_bytes()).keys, ())
        self.assertEqual(record.classifier_schema_version, index.schema_version)
        self.assertEqual(record.classifier_digest, index.digest)
        metadata = json.loads((record.generation_dir / "generation.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["classifier_schema_version"], index.schema_version)
        self.assertEqual(metadata["classifier_digest"], index.digest)

    def test_native_dialogue_generation_rejects_unavailable_or_stale_index(self):
        from w3sub_app.w3strings_native import StringsFile, encode
        for language in ("en", "zh"):
            for path in self.game.language_files[language]:
                path.write_bytes(encode(StringsFile(164, 0, ((1, language),), ())))
        index = self.fixture_index()
        (self.game_root / "refs" / "scene.json").write_text("changed references", encoding="utf-8")
        for unavailable in (None, index):
            with self.subTest(index=unavailable):
                with patch.object(generation, "load_dialogue_index", side_effect=lambda game:
                                  unavailable if unavailable is not None and unavailable.is_current(game) else None):
                    with self.assertRaisesRegex(GenerationError, "current validated dialogue index"):
                        generate(self.request(mode=MergeMode.DIALOGUE_ONLY), self.state_root)
        self.assertFalse(self.state_root.exists())

    def test_changed_index_digest_during_generation_cannot_publish_record(self):
        first = self.fixture_index()
        second = DialogueIndex.from_validated_references(
            self.game, [self.game_root / "refs" / "scene.json"],
            {("1", "0"): [DialogContext.SCENE_SUBTITLE], ("2", "0"): [DialogContext.ITEM]},
            source_roots=[self.game_root / "refs"], source_patterns=["scene*.json"])
        self.assertNotEqual(first.digest, second.digest)
        with patch.object(generation, "load_dialogue_index", side_effect=[first, second]):
            with self.assertRaisesRegex(GenerationError, "index changed during generation"):
                generate(self.request(mode=MergeMode.DIALOGUE_ONLY), self.state_root, self.converter)
        self.assertEqual(list(self.state_root.rglob("generation.json")), [])
        self.assertIsNone(generation.load_latest_generation_record(self.state_root, self.game.root))

    def test_changed_index_schema_during_generation_cannot_publish_record(self):
        index = FakeDialogueIndex()

        class SchemaChangingConverter(FakeConverter):
            def encode(self, csv_path, work_dir):
                output = super().encode(csv_path, work_dir)
                index.schema_version = 1
                return output

        with patch.object(generation, "load_dialogue_index", return_value=index):
            with self.assertRaisesRegex(GenerationError, "index changed during generation"):
                generate(self.request(mode=MergeMode.DIALOGUE_ONLY), self.state_root,
                         SchemaChangingConverter(self.executable))
        self.assertEqual(list(self.state_root.rglob("generation.json")), [])

    def test_default_native_generation_preserves_newlines_and_codec_identity(self):
        from w3sub_app.w3strings_native import StringsFile, decode, encode
        for language in ("en", "zh"):
            for path in self.game.language_files[language]:
                Path(path).write_bytes(encode(StringsFile(164, 0, ((1, language + "\r\n中\n\r"),), ((7, 1), (8, 1)))))
        before = {p: Path(p).read_bytes() for paths in self.game.language_files.values() for p in paths}
        record = generate(GenerationRequest(self.game, "en", "zh", MergeMode.FULL_TEXT), self.state_root)
        for output in record.output_files.values():
            result = decode(Path(output).read_bytes())
            self.assertEqual(result.strings, ((1, "en\r\n中\n\r<br>zh\r\n中\n\r"),))
            self.assertEqual(result.keys, ((7, 1), (8, 1)))
        self.assertEqual(record.codec_kind, "native")
        self.assertEqual(record.converter_sha256, hashlib.sha256(Path(record.converter_path).read_bytes()).hexdigest())
        self.assertEqual(generation.load_latest_generation_record(self.state_root, self.game.root), record)
        self.assertTrue(all(Path(p).read_bytes() == data for p, data in before.items()))

    def test_native_generation_is_stale_when_codec_identity_changes(self):
        from w3sub_app.w3strings_native import StringsFile, encode
        for language in ("en", "zh"):
            for path in self.game.language_files[language]:
                Path(path).write_bytes(encode(StringsFile(164, 0, ((1, language),), ((7, 1),))))
        record = generate(self.request(), self.state_root)
        previous_codec = replace(record, converter_sha256="0" * 64)

        self.assertEqual(compare_generation(previous_codec, self.game), Freshness.STALE)

    def test_default_native_generation_rejects_corrupt_resource(self):
        request = GenerationRequest(self.game, "en", "zh", MergeMode.FULL_TEXT)
        with self.assertRaisesRegex(GenerationError, "compatibility"):
            generate(request, self.state_root)

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
        self.assertEqual(metadata["generation_dir"], str(record.generation_dir))
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
        with patch.object(generation, "load_dialogue_index", side_effect=lambda game:
                          index if index.is_current(game) else None):
            with self.assertRaisesRegex(GenerationError, "index changed during generation"):
                generate(self.request(mode=MergeMode.DIALOGUE_ONLY),
                         self.state_root, self.converter)

    def test_real_dialogue_index_survives_same_supported_build_metadata_and_stays_metadata_only(self):
        source = self.game_root / "structured" / "scene-references.json"
        source.parent.mkdir(parents=True)
        source.write_text('{"fixture": true}\n', encoding="utf-8")
        index = DialogueIndex.from_validated_references(
            self.game,
            [source],
            {("1", "00000001"): [DialogContext.SCENE_SUBTITLE]},
            source_roots=[source.parent],
            source_patterns=["scene-*.json"],
        )
        updated_game = GameInstallation(
            self.game.root,
            self.game.storefront,
            GameVersion("5.0.0.1044393", "build-2", "5.0.0.1044393 (updated)"),
            self.game.language_files,
        )

        self.assertTrue(index.is_current(updated_game))
        with patch.object(generation, "load_dialogue_index", return_value=index):
            record = generate(self.request(mode=MergeMode.DIALOGUE_ONLY),
                              self.state_root, self.converter)
        self.assertEqual(record.game_version, self.game.version)
        self.assertEqual(record.classifier_digest, index.digest)
        with patch.object(generation, "load_dialogue_index", return_value=index):
            self.assertEqual(compare_generation(record, updated_game),
                             Freshness.VERSION_METADATA_CHANGED_ONLY)

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

    def test_modify_can_restore_a_secondary_input_from_a_prior_dual_install(self):
        secondary = self.game.language_files["zh"][0]
        secondary.write_text("1|00000001||zh previous<br>en previous\n", encoding="utf-8")
        backup = self.base / "backup" / "zh.w3strings"
        backup.parent.mkdir()
        backup.write_text("1|00000001||zh baseline\n", encoding="utf-8")
        relative = "content/content0/zh.w3strings"
        overrides = {relative: backup}

        record = generate(self.request(overrides=overrides), self.state_root, self.converter)
        output = Path(record.output_files["content/content0/en.w3strings"])
        merged_text = output.read_text(encoding="utf-8")

        self.assertEqual(merged_text, "1|00000001||en text<br>zh baseline\n")
        self.assertEqual(merged_text.count("<br>"), 1)
        self.assertEqual(record.source_fingerprint.entries[relative],
                         hashlib.sha256(backup.read_bytes()).hexdigest())
        self.assertEqual(compare_generation(record, self.game, overrides), Freshness.CURRENT)
        self.assertEqual(compare_generation(record, self.game), Freshness.STALE)

    def test_generation_output_paths_must_match_their_relative_keys_and_directory(self):
        record = generate(self.request(), self.state_root, self.converter)
        key = "content/content0/en.w3strings"
        output = Path(record.output_files[key])
        escaped = self.base / "outside.w3strings"
        escaped.write_bytes(output.read_bytes())

        escaped_record = replace(record, output_files={key: str(escaped)})
        self.assertEqual(compare_generation(escaped_record, self.game), Freshness.STALE)

        mismatched_key = "content/content0/zh.w3strings"
        mismatched_record = replace(
            record,
            output_files={mismatched_key: str(output)},
            output_hashes={mismatched_key: record.output_hashes[key]},
        )
        self.assertEqual(compare_generation(mismatched_record, self.game), Freshness.STALE)

    def test_partial_output_inventory_cannot_pass_as_a_complete_generation(self):
        extra_primary = self.write_language("en", "content/dlc0")
        extra_secondary = self.write_language("zh", "content/dlc0")
        game = GameInstallation(
            self.game.root,
            self.game.storefront,
            self.game.version,
            {
                "en": (*self.game.language_files["en"], extra_primary),
                "zh": (*self.game.language_files["zh"], extra_secondary),
            },
        )
        record = generate(self.request(game=game), self.state_root, self.converter)
        self.assertEqual(len(record.output_files), 2)

        missing_target = "content/dlc0/en.w3strings"
        partial_record = replace(
            record,
            output_files={key: path for key, path in record.output_files.items()
                          if key != missing_target},
            output_hashes={key: digest for key, digest in record.output_hashes.items()
                           if key != missing_target},
        )

        self.assertEqual(compare_generation(partial_record, game), Freshness.STALE)

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
