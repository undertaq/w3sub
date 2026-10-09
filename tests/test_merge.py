import tempfile
import unittest
from pathlib import Path

from w3sub_app.merge import MergeError, merge_csv, merge_records
from w3sub_app.models import MergeMode
from w3sub_app.w3strings_native import StringsFile

class NativeMergeTests(unittest.TestCase):
    def test_keyless_shared_spoken_id_merges_by_id(self):
        primary = StringsFile(164, 17, ((9, "ZH\r\n台詞"), (1, "only primary")), ((99, 90),))
        secondary = StringsFile(164, 23, ((9, "EN\nline"), (2, "only secondary")), ())
        result = merge_records(primary, secondary, MergeMode.DIALOGUE_ONLY)
        self.assertEqual(result.strings, ((9, "ZH\r\n台詞<br>EN\nline"), (1, "only primary")))
        self.assertEqual(result.keys, ((99, 90),))
        self.assertEqual((result.version, result.language_key), (164, 17))
        self.assertEqual(merge_records(primary, secondary, MergeMode.FULL_TEXT), primary)

    def test_keyless_fallback_requires_both_language_key_sets_empty(self):
        for primary_keys, secondary_keys, expected in (
                ((), (), "ZH<br>EN"), (((0, 1),), (), "ZH"),
                ((), ((0, 1),), "ZH"), (((7, 1),), ((8, 1),), "ZH")):
            with self.subTest(primary_keys=primary_keys, secondary_keys=secondary_keys):
                primary = StringsFile(164, 0, ((1, "ZH"),), primary_keys)
                secondary = StringsFile(164, 0, ((1, "EN"),), secondary_keys)
                result = merge_records(primary, secondary, MergeMode.DIALOGUE_ONLY)
                self.assertEqual(result.strings, ((1, expected),))
                self.assertEqual(result.keys, primary_keys)
        primary = StringsFile(164, 0, ((1, "ZH"),), ())
        missing = StringsFile(164, 0, ((2, "EN"),), ())
        self.assertEqual(merge_records(primary, missing, MergeMode.DIALOGUE_ONLY), primary)
        secondary = StringsFile(164, 0, ((1, "EN"),), ())
        self.assertEqual(merge_records(primary, secondary, MergeMode.DIALOGUE_ONLY).strings,
                         ((1, "ZH<br>EN"),))

    def test_matches_shared_real_keys_and_preserves_all_primary_associations(self):
        from w3sub_app.merge import merge_records
        from w3sub_app.w3strings_native import StringsFile
        primary = StringsFile(164, 0, ((1, "a\r\nb"), (2, "unkeyed"), (3, "different")),
                              ((10, 1), (11, 1), (12, 3), (99, 90)))
        secondary = StringsFile(163, 0, ((1, "中\n文\r"), (2, "other"), (3, "other")),
                                ((11, 1), (13, 3)))
        result = merge_records(primary, secondary, MergeMode.FULL_TEXT)
        self.assertEqual(result.strings, ((1, "a\r\nb<br>中\n文\r"), (2, "unkeyed"), (3, "different")))
        self.assertEqual(result.keys, primary.keys)
        self.assertEqual(result.version, 164)
        self.assertEqual(result.language_key, 0)
        self.assertEqual(merge_records(primary, secondary, MergeMode.DIALOGUE_ONLY), primary)

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

    def test_dialogue_only_csv_requires_shared_key_hash(self):
        original = "; primary\r\nid|key(hex)|key(str)|text\r\n1|00000001||ZH\r\n"
        self.write_pair(original, "1|00000002||EN\n")
        result = merge_csv(self.primary, self.secondary, MergeMode.DIALOGUE_ONLY)
        with result.open(encoding="utf-8", newline="") as output:
            self.assertEqual(output.read(), original)

    def test_full_text_preserves_metadata_order_key_and_pipe_text(self):
        primary = "; language en\r\nid|key(hex)|key(str)|text\r\n2|00000002|named|A|B<br>C\r\n; between\r\n1|00000001||first\r\n3|00000003||only primary\r\n1|00000004||different key\r\n"
        self.write_pair(primary,
                        "; zh\n1|00000001||一\n2|00000002|other|甲|乙<br>丙\n9|00000009||only secondary\n")
        result = merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT)
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
                        merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT)

    def test_empty_text_and_final_record_without_newline(self):
        self.write_pair("; en\n1|00000001||", "; zh\n1|00000001||other")
        result = merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT)
        self.assertEqual(result.read_text(encoding="utf-8"), "; en\n1|00000001||<br>other")

    def test_dialogue_only_preserves_keyed_csv_rows(self):
        self.write_pair("1|00000001||one\n", "1|00000001||two\n")
        result = merge_csv(self.primary, self.secondary, MergeMode.DIALOGUE_ONLY)
        self.assertEqual(result.read_text(encoding="utf-8"), "1|00000001||one\n")

    def test_bad_mode_or_unreadable_input_is_merge_error(self):
        self.write_pair("1|00000001||one\n", "1|00000001||two\n")
        with self.assertRaises(MergeError):
            merge_csv(self.primary, self.secondary, "unknown")
        self.secondary.unlink()
        with self.assertRaises(MergeError):
            merge_csv(self.primary, self.secondary, MergeMode.FULL_TEXT)
