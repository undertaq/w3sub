"""Independent binary fixtures for the built-in codec."""
import struct
import unittest
import tempfile
from pathlib import Path
from dataclasses import replace

from w3sub_app import w3strings_native as native


def fixture(version=164, text=b"A\r\nB\nC\rD", keys=((7, 1), (8, 1))):
    # Zero language key means cleartext; all section counts here fit one byte.
    unit = 1 if version == 164 else 2
    return (b"RTSW" + struct.pack("<IH", version, 0) + b"\x01"
            + struct.pack("<III", 1, 0, len(text) // unit)
            + bytes([len(keys)])
            + b"".join(struct.pack("<II", *key) for key in keys)
            + bytes([len(text) // unit + 1]) + text + bytes(unit) + b"\0\0")


class NativeCodecTests(unittest.TestCase):
    def test_native_probe_rejects_newline_mutation_and_only_reads_copies(self):
        from w3sub_app.converter import check_compatibility
        class MutatingCodec(native.NativeW3StringsCodec):
            def __init__(self):
                self.read_paths = []
            def decode(self, source, work_dir):
                self.read_paths.append(Path(source))
                return super().decode(source, work_dir)
            def encode(self, record, work_dir):
                changed = replace(record, strings=tuple((i, text.replace("\r\n", "\n")) for i,text in record.strings))
                return super().encode(changed, work_dir)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "en.w3strings"
            original = fixture()
            source.write_bytes(original)
            codec = MutatingCodec()
            report = check_compatibility({"content/en.w3strings": source}, codec, root / "probe")
            self.assertFalse(report.compatible)
            self.assertIn("semantic records changed", report.error)
            self.assertEqual(report.checked_resources, 1)
            self.assertTrue(all(path != source for path in codec.read_paths))
            self.assertEqual(source.read_bytes(), original)

    def test_known_bytes_and_exact_newlines_for_all_versions(self):
        for version in (162, 163, 164):
            with self.subTest(version=version):
                text = "A\r\nB\nC\rD中😀"
                raw = fixture(version, text.encode("utf-8" if version == 164 else "utf-16-le"))
                record = native.decode(raw)
                self.assertEqual(record.version, version)
                self.assertEqual(record.language_key, 0)
                self.assertEqual(record.strings, ((1, text),))
                self.assertEqual(record.keys, ((7, 1), (8, 1)))
                self.assertEqual(native.decode(native.encode(record)), record)

    def test_obfuscation_known_english_bytes(self):
        # English magic 79321793, id 1 xor magic, rotating initial key 3217.
        raw = (b"RTSW" + struct.pack("<IH", 164, 0x4397) + b"\x01"
               + struct.pack("<III", 0x79321792, 0, 1) + b"\x00\x02"
               + b"o\x00" + struct.pack("<H", 0x5139))
        self.assertEqual(native.decode(raw).strings, ((1, "A"),))
        self.assertEqual(native.encode(native.decode(raw)), raw)

    def test_obfuscated_string_index_is_sorted_for_game_lookup(self):
        record = native.StringsFile(164, 0x18632176,
                                    ((1, "one"), (2, "two"), (3, "three"), (4, "four")), ())
        raw = native.encode(record)
        reader = native._Reader(raw)
        reader.take(10)
        count = reader.count()
        stored_ids = [struct.unpack("<I", reader.take(12)[:4])[0]
                      for _ in range(count)]

        self.assertEqual(stored_ids, sorted(stored_ids))

    def test_rejects_duplicate_exact_association(self):
        with self.assertRaises(native.NativeCodecError):
            native.decode(fixture(keys=((7, 1), (7, 1))))

    def test_rejects_invalid_utf8_unknown_version_and_language(self):
        for raw in (fixture(text=b"\xff"), fixture(version=165),
                    fixture()[:8] + b"\x01\x00" + fixture()[10:]):
            with self.subTest(raw=raw):
                with self.assertRaises(native.NativeCodecError):
                    native.decode(raw)

    def test_truncated_and_out_of_bounds_containers_fail_closed(self):
        raw = fixture()
        for end in range(len(raw)):
            with self.subTest(end=end):
                with self.assertRaises(native.NativeCodecError):
                    native.decode(raw[:end])
        bad = bytearray(raw)
        struct.pack_into("<I", bad, 15, 1000)
        with self.assertRaises(native.NativeCodecError):
            native.decode(bytes(bad))

    def test_large_counts_roundtrip(self):
        record = native.StringsFile(164, 0, tuple((i, "x" * 130) for i in range(9000)), ())
        self.assertEqual(native.decode(native.encode(record)), record)

    def test_invalid_records_fail_closed(self):
        records = [native.StringsFile(164, 0, ((1, "a"), (1, "b")), ()),
                   native.StringsFile(164, 0, ((-1, "a"),), ()),
                   native.StringsFile(164, 0, ((1, "a"),), ((-4, 2),)),
                   native.StringsFile(164, 0, ((1, "\ud800"),), ())]
        for record in records:
            with self.assertRaises(native.NativeCodecError):
                native.encode(record)

    def test_associations_to_other_resource_ids_are_preserved(self):
        record = native.decode(fixture(keys=((7, 1), (8, 99))))
        self.assertEqual(record.keys, ((7, 1), (8, 99)))
        self.assertEqual(native.decode(native.encode(record)), record)


if __name__ == "__main__":
    unittest.main()
