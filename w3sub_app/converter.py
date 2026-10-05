"""Run the bundled converter on isolated staging copies."""
from pathlib import Path
from dataclasses import dataclass
import shutil
import subprocess
import tempfile
from pathlib import PurePosixPath, PureWindowsPath

from .merge import MergeError, _Record, _read_csv
from .w3strings_native import NativeW3StringsCodec
from .progress import ProgressCallback, report_progress


class ConverterError(RuntimeError):
    """Conversion failed; no output may be used."""


@dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    checked_resources: int
    error: str | None = None


def _semantic_records(path: Path) -> tuple[tuple[tuple[str, str], str, str], ...]:
    rows = _read_csv(path)
    records = []
    for row in rows:
        if isinstance(row, _Record):
            key_string = row.prefix.split("|", 3)[2]
            records.append((row.identity, key_string, row.text))
    return tuple(sorted(records))


def check_compatibility(sources: dict[str, Path], converter,
                        work_dir: Path, *,
                        progress_callback: ProgressCallback | None = None) -> CompatibilityReport:
    """Probe every selected resource by round-tripping isolated copies.

    Comparing parsed records ignores harmless comment/line-ending changes but
    requires identity, key-string metadata, and localized text to survive.
    A failed resource blocks the whole inventory; all resources are attempted
    so the UI can present the converter's complete actionable diagnostics.
    """
    if not isinstance(sources, dict) or not sources:
        return CompatibilityReport(False, 0, "No selected language resources to check")
    inventory = []
    for relative, raw_path in sorted(sources.items()):
        if not isinstance(relative, str) or not relative:
            return CompatibilityReport(False, 0, "Resource inventory contains an invalid path")
        posix, windows = PurePosixPath(relative), PureWindowsPath(relative)
        if (posix.is_absolute() or windows.is_absolute() or windows.drive
                or ".." in posix.parts or "." in posix.parts
                or posix.as_posix() != relative or "\\" in relative
                or posix.suffix.casefold() != ".w3strings"):
            return CompatibilityReport(False, 0,
                                       f"Unsafe resource path in compatibility inventory: {relative}")
        inventory.append((relative, Path(raw_path)))

    work_dir = Path(work_dir).expanduser().resolve()
    errors = []
    try:
        report_progress(progress_callback, "Checking resource compatibility", 0, len(inventory))
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="compatibility-", dir=work_dir) as probe_name:
            probe = Path(probe_name)
            for number, (relative, original) in enumerate(inventory):
                resource_dir = probe / f"resource-{number:04d}"
                staged = resource_dir / Path(relative).name
                try:
                    resource_dir.mkdir(parents=True, exist_ok=False)
                    shutil.copyfile(original, staged)
                    decoded = converter.decode(staged, resource_dir / "decode-original")
                    encoded = Path(converter.encode(decoded, resource_dir / "encode"))
                    roundtrip = converter.decode(encoded, resource_dir / "decode-roundtrip")
                    before = decoded if isinstance(converter, NativeW3StringsCodec) else _semantic_records(Path(decoded))
                    after = roundtrip if isinstance(converter, NativeW3StringsCodec) else _semantic_records(Path(roundtrip))
                    if before != after:
                        raise ConverterError(
                            "semantic records changed during decode/encode/decode round-trip"
                        )
                except Exception as error:
                    errors.append(f"{relative}: {error}")
                report_progress(progress_callback, "Checking resource compatibility",
                                number + 1, len(inventory))
    except Exception as error:
        errors.append(f"Cannot run converter compatibility check: {error}")
    return CompatibilityReport(not errors, len(inventory), "\n".join(errors) if errors else None)


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
                                    text=True, check=False, shell=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
