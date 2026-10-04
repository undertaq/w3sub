import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from w3sub_app.bundle_reader import (
    BundleReadError,
    enumerate_witcher_bundles,
    iter_bundle_entries,
    read_bundle_entry,
)


def _bundle_bytes(entries, *, version=5, truncate_metadata=False):
    """Build a tiny POTATO70 v5 archive from (name, payload, method) rows."""
    packed_rows = []
    for name, payload, method in entries:
        stored = zlib.compress(payload) if method == 1 else payload
        packed_rows.append((name.encode("utf-8"), payload, stored, method))

    metadata_size = 304 * len(packed_rows)
    next_offset = 32 + metadata_size
    rows = []
    payloads = []
    for name, payload, stored, method in packed_rows:
        row = bytearray(304)
        row[: min(len(name), 256)] = name[:256]
        struct.pack_into(
            "<QIIIB",
            row,
            272,
            next_offset,
            len(payload),
            len(stored),
            zlib.crc32(payload) & 0xFFFFFFFF,
            method,
        )
        rows.append(bytes(row))
        payloads.append(stored)
        next_offset += len(stored)

    header = bytearray(32)
    header[:8] = b"POTATO70"
    struct.pack_into("<Q", header, 8, next_offset)
    struct.pack_into("<I", header, 16, metadata_size)
    struct.pack_into("<H", header, 20, version)
    result = bytes(header) + b"".join(rows) + b"".join(payloads)
    if truncate_metadata:
        result = bytearray(result[: 32 + metadata_size - 1])
        struct.pack_into("<Q", result, 8, len(result))
    return bytes(result)


class BundleReaderTests(unittest.TestCase):
    def test_enumerates_content_and_optional_dlc_but_skips_tombstones(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = [
                root / "content" / "base" / "a.bundle",
                root / "dlc" / "ep1" / "z.BUNDLE",
            ]
            for path in expected:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            tombstone = root / "dlc-tombstones" / "deleted.bundle"
            nested_tombstone = root / "dlc" / "dlc-tombstones" / "deleted.bundle"
            for path in (tombstone, nested_tombstone):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

            self.assertEqual(enumerate_witcher_bundles(root), tuple(expected))

    def test_reads_v5_raw_and_zlib_entries_and_preserves_duplicate_paths(self):
        content = b"raw resource"
        compressed = b"zlib resource" * 5
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.bundle"
            path.write_bytes(
                _bundle_bytes(
                    [("same/path.w2scene", content, 0), ("same/path.w2scene", compressed, 1)]
                )
            )

            entries = tuple(iter_bundle_entries(path))
            self.assertEqual([entry.depot_path for entry in entries], ["same/path.w2scene"] * 2)
            self.assertEqual([entry.entry_index for entry in entries], [0, 1])
            self.assertEqual([read_bundle_entry(path, entry) for entry in entries], [content, compressed])

    def test_rejects_truncated_metadata_and_out_of_bounds_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.bundle"
            path.write_bytes(_bundle_bytes([("a", b"payload", 0)], truncate_metadata=True))
            with self.assertRaisesRegex(BundleReadError, "bad.bundle"):
                tuple(iter_bundle_entries(path))

            path.write_bytes(_bundle_bytes([("a", b"payload", 0)], version=3))
            with self.assertRaisesRegex(BundleReadError, "bad.bundle.*expected v5"):
                tuple(iter_bundle_entries(path))

            valid = bytearray(_bundle_bytes([("a", b"payload", 0)]))
            struct.pack_into("<Q", valid, 32 + 272, len(valid) + 100)
            path.write_bytes(valid)
            with self.assertRaisesRegex(BundleReadError, "bad.bundle"):
                tuple(iter_bundle_entries(path))

    def test_rejects_unknown_compression_size_or_crc_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.bundle"
            path.write_bytes(_bundle_bytes([("a", b"payload", 2)]))
            entry = next(iter_bundle_entries(path))
            with self.assertRaisesRegex(BundleReadError, "bad.bundle.*compression"):
                read_bundle_entry(path, entry)

            path.write_bytes(_bundle_bytes([("a", b"payload", 0)]))
            entry = next(iter_bundle_entries(path))
            damaged = bytearray(path.read_bytes())
            struct.pack_into("<I", damaged, 32 + 280, 100)
            path.write_bytes(damaged)
            entry = next(iter_bundle_entries(path))
            with self.assertRaisesRegex(BundleReadError, "bad.bundle.*size"):
                read_bundle_entry(path, entry)

            damaged = bytearray(_bundle_bytes([("a", b"payload", 0)]))
            struct.pack_into("<I", damaged, 32 + 288, 0)
            path.write_bytes(damaged)
            entry = next(iter_bundle_entries(path))
            with self.assertRaisesRegex(BundleReadError, "bad.bundle.*CRC"):
                read_bundle_entry(path, entry)


if __name__ == "__main__":
    unittest.main()
