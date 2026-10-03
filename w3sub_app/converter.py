"""Run the bundled converter on isolated staging copies."""
from pathlib import Path
import shutil
import subprocess
import tempfile


class ConverterError(RuntimeError):
    """Conversion failed; no output may be used."""


class W3StringsConverter:
    def __init__(self, executable: Path):
        self.executable = Path(executable).resolve()

    def decode(self, source: Path, work_dir: Path) -> Path:
        return self._convert(source, work_dir, "--decode", ".csv")

    def encode(self, csv_path: Path, work_dir: Path) -> Path:
        return self._convert(csv_path, work_dir, "--encode", ".w3strings")

    def _convert(self, source: Path, work_dir: Path, operation: str, suffix: str) -> Path:
        source = Path(source).resolve()
        work_dir = Path(work_dir).resolve()
        if not self.executable.is_file():
            raise ConverterError(f"Converter executable is missing: {self.executable}")
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            # Every call has fresh paths: stale outputs and same-named assets
            # from separate resource directories cannot be mistaken for success.
            stage_dir = Path(tempfile.mkdtemp(prefix="convert-", dir=work_dir))
            staged = stage_dir / source.name
            shutil.copyfile(source, staged)
            output = Path(str(staged) + suffix)
            arguments = [str(self.executable), operation, str(staged)]
            if operation == "--encode":
                arguments.append("--force-ignore-id-space-check-i-know-what-i-am-doing")
            result = subprocess.run(arguments, cwd=work_dir, capture_output=True,
                                    text=True, check=False, shell=False)
        except (OSError, UnicodeError) as error:
            raise ConverterError(f"Cannot {operation} {source}: {error}") from error
        diagnostics = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        if result.returncode != 0:
            raise ConverterError(f"Converter {operation} failed for {source} "
                                 f"(exit {result.returncode}).\n{diagnostics}")
        if not output.is_file() or output.stat().st_size == 0:
            raise ConverterError(f"Converter {operation} did not produce a nonempty "
                                 f"output at {output}.\n{diagnostics}")
        return output
