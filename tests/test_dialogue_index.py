import tempfile
import unittest
from pathlib import Path

from w3sub_app.dialogue_index import (
    DialogContext,
    DialogueIndex,
    build_dialogue_index,
    dialogue_index_unavailable_reason,
    load_dialogue_index,
)
from w3sub_app.game import fingerprint_files
from w3sub_app.models import GameInstallation, GameVersion, Storefront


class DialogueIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dialogue index ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "content" / "structured-fixture.json"
        self.source.parent.mkdir(parents=True)
        self.source.write_text('{"fixture": true}\n', encoding="utf-8")
        self.game = self.make_game()

    def make_game(self, version="5.0.0.1044392"):
        return GameInstallation(
            root=self.root,
            storefront=Storefront.STEAM,
            version=GameVersion(version, "fixture-build", version),
            language_files={},
        )

    def make_index(self, references):
        return DialogueIndex.from_validated_references(
            self.game, [self.source], references,
            source_roots=[self.source.parent], source_patterns=["*.json"],
        )

    def test_classifies_only_confirmed_reference_contexts(self):
        index = self.make_index({
            ("100", "A001"): [DialogContext.SCENE_SUBTITLE],
            ("200", "B002"): [DialogContext.OVERHEAD],
            ("300", "C003"): [DialogContext.ITEM],
            ("400", "D004"): [DialogContext.OBJECTIVE],
            ("500", "E005"): [DialogContext.SCENE_SUBTITLE, DialogContext.OVERHEAD],
            ("600", "F006"): [DialogContext.HUD_UI],
        })

        self.assertEqual(index.context_for("100", "a001"), DialogContext.SCENE_SUBTITLE)
        self.assertEqual(index.context_for("200", "B002"), DialogContext.OVERHEAD)
        self.assertEqual(index.context_for("300", "C003"), DialogContext.ITEM)
        self.assertEqual(index.context_for("400", "D004"), DialogContext.OBJECTIVE)
        self.assertEqual(index.context_for("500", "E005"), DialogContext.AMBIGUOUS)
        self.assertEqual(index.context_for("600", "F006"), DialogContext.HUD_UI)
        self.assertEqual(index.context_for("700", "0700"), DialogContext.UNKNOWN)
        self.assertEqual(index.context_for("100", "wrong-key"), DialogContext.UNKNOWN)

    def test_padded_hash_spelling_resolves_same_identity_and_digest(self):
        padded = self.make_index({("001", "0000000A"): [DialogContext.SCENE_SUBTITLE]})
        plain = self.make_index({("1", "a"): [DialogContext.SCENE_SUBTITLE]})
        for spelling in ("a", "A", "0000000a", "000A"):
            self.assertEqual(padded.context_for("1", spelling), DialogContext.SCENE_SUBTITLE)
            self.assertEqual(plain.context_for("001", spelling), DialogContext.SCENE_SUBTITLE)
        self.assertEqual(padded.digest, plain.digest)

    def test_equivalent_hash_spellings_union_conflicting_contexts_for_native_merge(self):
        from w3sub_app.merge import merge_records
        from w3sub_app.models import MergeMode
        from w3sub_app.w3strings_native import StringsFile
        index = self.make_index({
            ("1", "a"): [DialogContext.SCENE_SUBTITLE],
            ("01", "0000000A"): [DialogContext.ITEM],
        })
        self.assertTrue(index.validated)
        for spelling in ("a", "0000000a", "000A"):
            self.assertEqual(index.context_for("1", spelling), DialogContext.AMBIGUOUS)
        primary = StringsFile(164, 0, ((1, "primary"),), ((10, 1),))
        secondary = StringsFile(164, 0, ((1, "secondary"),), ((10, 1),))
        self.assertEqual(merge_records(primary, secondary, MergeMode.DIALOGUE_ONLY, index, self.game), primary)

    def test_native_merge_accepts_padded_only_confirmed_subtitle(self):
        from w3sub_app.merge import merge_records
        from w3sub_app.models import MergeMode
        from w3sub_app.w3strings_native import StringsFile
        index = self.make_index({("1", "0000000a"): [DialogContext.SCENE_SUBTITLE]})
        primary = StringsFile(164, 0, ((1, "primary"),), ((10, 1),))
        secondary = StringsFile(164, 0, ((1, "secondary"),), ((10, 1),))
        result = merge_records(primary, secondary, MergeMode.DIALOGUE_ONLY, index, self.game)
        self.assertEqual(result.strings, ((1, "primary<br>secondary"),))
        self.assertEqual(result.keys, primary.keys)

    def test_digest_binds_version_fingerprint_schema_and_contexts(self):
        scene = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})
        overhead = self.make_index({("100", "A001"): [DialogContext.OVERHEAD]})
        other_version = DialogueIndex.from_validated_references(
            self.make_game("5.0.0.1044393"), [self.source],
            {("100", "A001"): [DialogContext.SCENE_SUBTITLE]},
            source_roots=[self.source.parent], source_patterns=["*.json"],
        )

        self.assertTrue(scene.validated)
        self.assertEqual(scene.schema_version, 2)
        self.assertEqual(scene.game_version, self.game.version)
        self.assertEqual(scene.source_fingerprint, fingerprint_files(self.root, [self.source]))
        self.assertNotEqual(scene.digest, overhead.digest)
        self.assertNotEqual(scene.digest, other_version.digest)

    def test_metadata_version_changes_preserve_index_but_source_changes_invalidate_it(self):
        index = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})

        self.assertTrue(index.is_current(self.game))
        self.assertTrue(index.is_current(self.make_game("5.0.0.1044393")))
        self.assertFalse(index.is_current(self.make_game("6.0.0.1044393")))
        self.source.write_text('{"fixture": changed}\n', encoding="utf-8")
        self.assertFalse(index.is_current(self.game))

    def test_missing_or_unreadable_referenced_source_is_stale(self):
        index = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})
        self.source.unlink()

        self.assertFalse(index.is_current(self.game))

    def test_added_matching_source_invalidates_index_but_unmatched_file_does_not(self):
        index = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})
        unrelated = self.source.parent / "readme.txt"
        unrelated.write_text("not a structured source", encoding="utf-8")
        self.assertTrue(index.is_current(self.game))

        added = self.source.parent / "new-reference.json"
        added.write_text('{"use": "same id in another context"}\n', encoding="utf-8")

        self.assertFalse(index.is_current(self.game))

    def test_declared_source_files_must_match_scoped_inventory(self):
        additional = self.source.parent / "unreported-reference.json"
        additional.write_text("{}\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})

    def test_inventory_scope_rejects_game_wide_unfiltered_scan(self):
        with self.assertRaises(ValueError):
            DialogueIndex.from_validated_references(
                self.game, [self.source],
                {("100", "A001"): [DialogContext.SCENE_SUBTITLE]},
                source_roots=[self.root], source_patterns=["*"],
            )

    def test_no_supported_live_source_means_no_index(self):
        self.assertIsNone(load_dialogue_index(self.game))
        self.assertIsNone(build_dialogue_index(self.game))
        reason = dialogue_index_unavailable_reason(self.game)
        self.assertIn("no active packed bundle data", reason)
        self.assertIn("Full-text merge remains available", reason)


if __name__ == "__main__":
    unittest.main()
