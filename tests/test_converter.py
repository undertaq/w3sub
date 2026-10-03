import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from w3sub_app.converter import ConverterError, W3StringsConverter


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="converter space ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "converter space.exe"
        self.exe.write_bytes(b"fixture executable")
        self.source = self.root / "source space.w3strings"
        self.source.write_bytes(b"original")
        self.work = self.root / "work space"
        self.converter = W3StringsConverter(self.exe)

    def test_decode_copies_source_and_uses_safe_absolute_arguments(self):
        def run(args, **kwargs):
            self.assertEqual(args[0], str(self.exe.resolve()))
            self.assertEqual(args[1], "--decode")
            staged = Path(args[2])
            self.assertTrue(staged.is_absolute())
            self.assertTrue(staged.is_relative_to(self.work))
            self.assertNotEqual(staged, self.source)
            self.assertEqual(staged.read_bytes(), b"original")
            self.assertEqual(kwargs, dict(cwd=self.work.resolve(), capture_output=True,
                                          text=True, check=False, shell=False))
            Path(str(staged) + ".csv").write_text("; en\n1|00000000||hello\n")
            return subprocess.CompletedProcess(args, 0, "decoded", "")
        with patch("w3sub_app.converter.subprocess.run", side_effect=run):
            result = self.converter.decode(self.source, self.work)
        self.assertTrue(result.is_file())
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertFalse(Path(str(self.source) + ".csv").exists())

    def test_encode_uses_bypass_and_stages_without_overwriting_input(self):
        csv = self.root / "primary space.csv"
        csv.write_text("; en\n1|00000000||hello\n")
        def run(args, **kwargs):
            self.assertEqual(args[1], "--encode")
            self.assertIn("--force-ignore-id-space-check-i-know-what-i-am-doing", args)
            staged = Path(args[2])
            self.assertNotEqual(staged, csv)
            self.assertEqual(staged.read_bytes(), csv.read_bytes())
            Path(str(staged) + ".w3strings").write_bytes(b"encoded")
            return subprocess.CompletedProcess(args, 0, "encoded", "")
        with patch("w3sub_app.converter.subprocess.run", side_effect=run):
            result = self.converter.encode(csv, self.work)
        self.assertEqual(result.read_bytes(), b"encoded")
        self.assertFalse(Path(str(csv) + ".w3strings").exists())

    def test_nonzero_exit_includes_both_diagnostics(self):
        with patch("w3sub_app.converter.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 101, "version 164", "panic")):
            with self.assertRaises(ConverterError) as error:
                self.converter.decode(self.source, self.work)
        for part in ("101", "version 164", "panic"):
            self.assertIn(part, str(error.exception))

    def test_success_without_output_is_rejected_even_if_work_has_stale_csv(self):
        self.work.mkdir()
        (self.work / (self.source.name + ".csv")).write_text("stale")
        with patch("w3sub_app.converter.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, "no output", "diagnostic")):
            with self.assertRaises(ConverterError) as error:
                self.converter.decode(self.source, self.work)
        self.assertIn("no output", str(error.exception))
        self.assertIn("diagnostic", str(error.exception))

    def test_missing_executable_and_launch_error_are_actionable(self):
        self.exe.unlink()
        with self.assertRaises(ConverterError):
            self.converter.decode(self.source, self.work)
        self.exe.write_bytes(b"fixture")
        with patch("w3sub_app.converter.subprocess.run", side_effect=OSError("cannot launch")):
            with self.assertRaisesRegex(ConverterError, "cannot launch"):
                self.converter.decode(self.source, self.work)
