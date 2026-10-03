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
            self.game, [self.source], references
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

    def test_digest_binds_version_fingerprint_schema_and_contexts(self):
        scene = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})
        overhead = self.make_index({("100", "A001"): [DialogContext.OVERHEAD]})
        other_version = DialogueIndex.from_validated_references(
            self.make_game("5.0.0.1044393"), [self.source],
            {("100", "A001"): [DialogContext.SCENE_SUBTITLE]},
        )

        self.assertTrue(scene.validated)
        self.assertEqual(scene.schema_version, 1)
        self.assertEqual(scene.game_version, self.game.version)
        self.assertEqual(scene.source_fingerprint, fingerprint_files(self.root, [self.source]))
        self.assertNotEqual(scene.digest, overhead.digest)
        self.assertNotEqual(scene.digest, other_version.digest)

    def test_source_fingerprint_or_game_version_mismatch_is_stale(self):
        index = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})

        self.assertTrue(index.is_current(self.game))
        self.assertFalse(index.is_current(self.make_game("5.0.0.1044393")))
        self.source.write_text('{"fixture": changed}\n', encoding="utf-8")
        self.assertFalse(index.is_current(self.game))

    def test_missing_or_unreadable_referenced_source_is_stale(self):
        index = self.make_index({("100", "A001"): [DialogContext.SCENE_SUBTITLE]})
        self.source.unlink()

        self.assertFalse(index.is_current(self.game))

    def test_no_supported_live_source_means_no_index(self):
        self.assertIsNone(load_dialogue_index(self.game))
        self.assertIsNone(build_dialogue_index(self.game))
        reason = dialogue_index_unavailable_reason(self.game)
        self.assertIn("packed bundle data", reason)
        self.assertIn("Full-text merge remains available", reason)


if __name__ == "__main__":
    unittest.main()
