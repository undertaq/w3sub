import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from w3sub_app import game
from w3sub_app.models import Storefront


class GameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "game"
        (self.root / "content").mkdir(parents=True)

    def asset(self, relative, data=b"fixture"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, version="5.0.0.1044392", build=None):
        return game.scan_game(self.root, Storefront.STEAM, build,
                              version_reader=lambda path: version)

    def test_x64_supported_without_dlc_preserves_version_and_build(self):
        self.asset("bin/x64/witcher3.exe")
        self.asset("content/en.w3strings")
        result = self.scan(build="25646871")
        self.assertEqual(result.root, self.root.resolve())
        self.assertEqual(result.storefront, Storefront.STEAM)
        self.assertEqual(result.version.executable_version, "5.0.0.1044392")
        self.assertEqual(result.version.executable_version_raw, "5.0.0.1044392")
        self.assertEqual(result.version.store_build_id, "25646871")
        self.assertEqual(tuple(result.language_files), ("en",))

    def test_dx12_preferred_when_both_executables_exist(self):
        self.asset("bin/x64/witcher3.exe")
        dx12 = self.asset("bin/x64_dx12/witcher3.exe")
        self.assertEqual(game.find_game_executable(self.root), dx12)
        result = game.scan_game(self.root, Storefront.UNKNOWN,
                                version_reader=lambda path: "5.0.1.9" if path == dx12 else "4.0.0.0")
        self.assertEqual(result.version.executable_version, "5.0.1.9")
        self.assertIsNone(result.version.store_build_id)

    def test_rejects_classic_next_gen_and_malformed_versions(self):
        self.asset("bin/x64/witcher3.exe")
        for version in ("1.32", "4.0.0.1", "4.04", "5.1.0.0", "15.0.0.0", "5.0junk", "", "unknown"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.scan(version)

    def test_build_machine_annotation_preserves_complete_numeric_revision(self):
        self.asset("bin/x64/witcher3.exe")
        result = self.scan("5.0.0.1044392(Build Machine)")
        self.assertEqual(result.version.executable_version, "5.0.0.1044392")
        self.assertEqual(result.version.executable_version_raw, "5.0.0.1044392(Build Machine)")

    def test_native_resource_whitespace_preserved_in_raw_version(self):
        self.asset("bin/x64/witcher3.exe")
        raw = " \t5.0.0.1044392(Build Machine) \r\n"
        resource = ctypes.create_unicode_buffer(raw)
        translations = (wintypes.WORD * 2)(0x0409, 0x04b0)
        api = Mock()
        api.GetFileVersionInfoSizeW.return_value = 128
        api.GetFileVersionInfoW.return_value = True

        def query(buffer, key, pointer, length):
            if key == r"\VarFileInfo\Translation":
                value, count = translations, ctypes.sizeof(translations)
            elif key == r"\StringFileInfo\040904b0\FileVersion":
                value, count = resource, len(resource)
            else:
                return False
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(value, ctypes.c_void_p).value
            ctypes.cast(length, ctypes.POINTER(wintypes.UINT))[0] = count
            return True

        api.VerQueryValueW.side_effect = query
        with patch.object(game.ctypes, "WinDLL", return_value=api):
            result = game.scan_game(self.root, Storefront.UNKNOWN)
        self.assertEqual(result.version.executable_version_raw, raw)
        self.assertEqual(result.version.executable_version, "5.0.0.1044392")

    def test_unreadable_optional_resource_root_is_not_omitted(self):
        self.asset("bin/x64/witcher3.exe")
        self.asset("content/en.w3strings")
        self.asset("dlc/expansion/en.w3strings")
        dlc = self.root / "dlc"
        original_stat = os.stat
        for error in (PermissionError(13, "Access denied"), OSError(5, "I/O error")):
            def inaccessible(path, *args, **kwargs):
                if Path(path) == dlc:
                    raise error
                return original_stat(path, *args, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(os, "stat", inaccessible):
                with self.assertRaises(type(error)):
                    self.scan()

    def test_missing_required_content_or_executable_rejected(self):
        with self.assertRaises(FileNotFoundError):
            self.scan()
        self.asset("bin/x64/witcher3.exe")
        (self.root / "content").rmdir()
        with self.assertRaises(ValueError):
            self.scan()

    def test_language_inventory_sorted_and_limited_to_resource_roots(self):
        self.asset("bin/x64_dx12/witcher3.exe")
        paths = [self.asset("content/z/en.w3strings"), self.asset("content/a/en.w3strings")]
        dlc = self.asset("dlc/expansion/en.w3strings")
        self.asset("content/cn.w3strings")
        self.asset("dlc/expansion/esmx.w3strings")
        self.asset("content/JP.W3STRINGS")
        self.asset("dlc-tombstones/xx.w3strings")
        self.asset("mods/yy.w3strings")
        self.asset("content/invalid-name.w3strings")
        result = self.scan()
        self.assertEqual(tuple(result.language_files), ("cn", "en", "esmx", "jp"))
        self.assertEqual(result.language_files["en"], (paths[1], paths[0], dlc))

    def test_version_read_failure_is_not_accepted(self):
        self.asset("bin/x64/witcher3.exe")
        def unreadable(path):
            raise OSError("version unavailable")
        with self.assertRaises(OSError):
            game.scan_game(self.root, Storefront.UNKNOWN, version_reader=unreadable)

    def test_fingerprint_order_independent_with_file_hashes(self):
        a = self.asset("content/a.w3strings", b"abc")
        b = self.asset("content/b.w3strings", b"def")
        first = game.fingerprint_files(self.root, [b, a])
        self.assertEqual(first, game.fingerprint_files(self.root, [a, b]))
        self.assertEqual(tuple(first.entries), ("content/a.w3strings", "content/b.w3strings"))
        self.assertEqual(first.entries["content/a.w3strings"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(len(first.digest), 64)

    def test_fingerprint_detects_bytes_and_inventory_changes(self):
        a = self.asset("content/a.w3strings", b"abc")
        before = game.fingerprint_files(self.root, [a])
        a.write_bytes(b"def")
        self.assertNotEqual(before.digest, game.fingerprint_files(self.root, [a]).digest)
        b = self.asset("content/b.w3strings", b"abc")
        self.assertNotEqual(before.digest, game.fingerprint_files(self.root, [b]).digest)
        self.assertNotEqual(before.digest, game.fingerprint_files(self.root, []).digest)

    def test_fingerprint_rejects_missing_unreadable_or_outside_paths(self):
        a = self.asset("content/a.w3strings")
        with self.assertRaises(OSError):
            game.fingerprint_files(self.root, [a, self.root / "missing"])
        with self.assertRaises(OSError):
            game.fingerprint_files(self.root, [self.root / "content"])
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"abc")
        with self.assertRaises(ValueError):
            game.fingerprint_files(self.root, [outside])


if __name__ == "__main__":
    unittest.main()
