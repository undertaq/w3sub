import tempfile
import unittest
from pathlib import Path

from w3sub_app.dialogue_index import DialogContext, DialogueIndex
from w3sub_app.merge import MergeError, merge_csv
from w3sub_app.models import GameInstallation, GameVersion, MergeMode, Storefront


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge space ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.primary = self.root / "primary space.csv"
        self.secondary = self.root / "secondary space.csv"

    def write_pair(self, primary, secondary):
        self.primary.write_text(primary, encoding="utf-8", newline="")
        self.secondary.write_text(secondary, encoding="utf-8", newline="")

    def test_full_text_preserves_metadata_order_key_and_pipe_text(self):
        primary = "; language en\r\nid|key(hex)|key(str)|text\r\n2|00000002|named|A|B<br>C\r\n; between\r\n1|00000001||first\r\n3|00000003||only primary\r\n1|00000004||different key\r\n"
        self.write_pair(primary,
                        "; zh\n1|00000001||一\n2|00000002|other|甲|乙<br>丙\n9|00000009||only secondary\n")
        result = merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT, None)
        with result.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(handle.read(), "; language en\r\nid|key(hex)|key(str)|text\r\n2|00000002|named|A|B<br>C<br>甲|乙<br>丙\r\n; between\r\n1|00000001||first<br>一\r\n3|00000003||only primary\r\n1|00000004||different key\r\n")
        with self.primary.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(handle.read(), primary)

    def test_malformed_and_duplicate_rows_rejected_in_either_source(self):
        valid = "; en\n1|00000001||one\n"
        for bad in ("not a record\n", "1|00000001|missing text\n",
                    "|00000001||empty id\n", "1|||empty key\n",
                    "1|00000001||one\n1|00000001||two\n"):
            for side in ("primary", "secondary"):
                with self.subTest(bad=bad, side=side):
                    self.write_pair(bad if side == "primary" else valid,
                                    bad if side == "secondary" else valid)
                    with self.assertRaises(MergeError):
                        merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT, None)

    def test_empty_text_and_final_record_without_newline(self):
        self.write_pair("; en\n1|00000001||", "; zh\n1|00000001||other")
        result = merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT, None)
        self.assertEqual(result.read_text(encoding="utf-8"), "; en\n1|00000001||<br>other")

    def test_dialogue_only_without_index_preserves_primary_rows(self):
        self.write_pair("1|00000001||one\n", "1|00000001||two\n")
        for index in (None, object()):
            result = merge_csv(self.primary, self.secondary, MergeMode.DIALOGUE_ONLY, index)
            self.assertEqual(result.read_text(encoding="utf-8"), "1|00000001||one\n")

    def test_dialogue_only_merges_scene_rows_and_preserves_other_contexts(self):
        self.write_pair(
            "1|00000001||scene\n2|00000002||overhead\n3|00000003||item\n"
            "4|00000004||objective\n5|00000005||ambiguous\n6|00000006||hud\n"
            "7|00000007||other\n8|00000008||unknown\n",
            "1|00000001||scene-alt\n2|00000002||overhead-alt\n3|00000003||item-alt\n"
            "4|00000004||objective-alt\n5|00000005||ambiguous-alt\n6|00000006||hud-alt\n"
            "7|00000007||other-alt\n8|00000008||unknown-alt\n",
        )
        source = self.root / "scene-references.json"
        source.write_text('{"fixture": true}\n', encoding="utf-8")
        game = GameInstallation(
            self.root, Storefront.STEAM, GameVersion("5.0.0.1044392", "fixture", None), {}
        )
        index = DialogueIndex.from_validated_references(game, [source], {
            ("1", "00000001"): [DialogContext.SCENE_SUBTITLE],
            ("2", "00000002"): [DialogContext.OVERHEAD],
            ("3", "00000003"): [DialogContext.ITEM],
            ("4", "00000004"): [DialogContext.OBJECTIVE],
            ("5", "00000005"): [DialogContext.SCENE_SUBTITLE, DialogContext.OVERHEAD],
            ("6", "00000006"): [DialogContext.HUD_UI],
            ("7", "00000007"): [DialogContext.OTHER],
        })

        result = merge_csv(self.primary, self.secondary, MergeMode.DIALOGUE_ONLY,
                           index, current_game=game)

        self.assertEqual(
            result.read_text(encoding="utf-8"),
            "1|00000001||scene<br>scene-alt\n2|00000002||overhead\n"
            "3|00000003||item\n4|00000004||objective\n5|00000005||ambiguous\n"
            "6|00000006||hud\n7|00000007||other\n8|00000008||unknown\n",
        )

    def test_stale_index_preserves_all_primary_rows(self):
        self.write_pair("1|00000001||primary\n", "1|00000001||secondary\n")
        source = self.root / "scene-references.json"
        source.write_text("original source\n", encoding="utf-8")
        game = GameInstallation(
            self.root, Storefront.STEAM, GameVersion("5.0.0.1044392", "fixture", None), {}
        )
        index = DialogueIndex.from_validated_references(game, [source], {
            ("1", "00000001"): [DialogContext.SCENE_SUBTITLE],
        })
        source.write_text("changed source\n", encoding="utf-8")

        result = merge_csv(self.primary, self.secondary, MergeMode.DIALOGUE_ONLY,
                           index, current_game=game)

        self.assertEqual(result.read_text(encoding="utf-8"), "1|00000001||primary\n")

    def test_index_without_current_game_preserves_primary_rows(self):
        self.write_pair("1|00000001||primary\n", "1|00000001||secondary\n")
        source = self.root / "scene-references.json"
        source.write_text("unchanged source\n", encoding="utf-8")
        game = GameInstallation(
            self.root, Storefront.STEAM, GameVersion("5.0.0.1044392", "fixture", None), {}
        )
        index = DialogueIndex.from_validated_references(game, [source], {
            ("1", "00000001"): [DialogContext.SCENE_SUBTITLE],
        })

        result = merge_csv(self.primary, self.secondary, MergeMode.DIALOGUE_ONLY, index)

        self.assertEqual(result.read_text(encoding="utf-8"), "1|00000001||primary\n")

    def test_bad_mode_or_unreadable_input_is_merge_error(self):
        self.write_pair("1|00000001||one\n", "1|00000001||two\n")
        with self.assertRaises(MergeError):
            merge_csv(self.primary, self.secondary, "unknown", None)
        self.secondary.unlink()
        with self.assertRaises(MergeError):
            merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT, None)
