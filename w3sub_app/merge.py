"""Preserve converter CSV metadata and merge records by localization identity."""
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Protocol

from .models import MergeMode


class MergeError(RuntimeError):
    """CSV data or a requested merge policy cannot be safely used."""


class DialogueIndex(Protocol):
    """Task 4's validated index boundary; raw/unvalidated indexes fail closed."""

    validated: bool

    def context_for(self, string_id: str, key: str) -> object:
        ...


@dataclass(frozen=True)
class _Record:
    prefix: str
    identity: tuple[str, str]
    text: str
    newline: str


def _read_csv(path: Path) -> list[str | _Record]:
    rows: list[str | _Record] = []
    identities: set[tuple[str, str]] = set()
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip() or line.lstrip().startswith(";"):
                    rows.append(line)
                    continue
                body = line.rstrip("\r\n")
                newline = line[len(body):]
                columns = body.split("|", 3)
                if len(columns) == 4 and tuple(c.strip().lower() for c in columns[:3]) == (
                        "id", "key(hex)", "key(str)"):
                    rows.append(line)
                    continue
                if (len(columns) != 4 or not re.fullmatch(r"[0-9]+", columns[0].strip())
                        or not re.fullmatch(r"[0-9a-fA-F]+", columns[1].strip())):
                    raise MergeError(f"Malformed CSV record at {path}:{line_number}")
                identity = (str(int(columns[0].strip())), columns[1].strip().lower())
                if identity in identities:
                    raise MergeError(f"Ambiguous duplicate record {identity} at {path}:{line_number}")
                identities.add(identity)
                prefix = "|".join(columns[:3]) + "|"
                rows.append(_Record(prefix, identity, columns[3], newline))
    except (OSError, UnicodeError) as error:
        raise MergeError(f"Cannot read CSV {path}: {error}") from error
    return rows


def merge_csv(primary: Path, secondary: Path, mode: MergeMode,
              dialogue_index: DialogueIndex | None = None) -> Path:
    """Create a separate CSV beside primary; callers pair resource paths first."""
    if not isinstance(mode, MergeMode):
        raise MergeError(f"Unsupported merge mode: {mode}")
    if mode is MergeMode.DIALOGUE_ONLY and (
            getattr(dialogue_index, "validated", False) is not True
            or not callable(getattr(dialogue_index, "context_for", None))):
        raise MergeError("Dialogue-only merge requires a validated dialogue index")
    primary = Path(primary)
    secondary = Path(secondary)
    primary_rows = _read_csv(primary)
    secondary_records = {row.identity: row for row in _read_csv(secondary)
                         if isinstance(row, _Record)}
    lines: list[str] = []
    for row in primary_rows:
        if isinstance(row, str):
            lines.append(row)
            continue
        other = secondary_records.get(row.identity)
        combine = other is not None
        if combine and mode is MergeMode.DIALOGUE_ONLY:
            assert dialogue_index is not None
            context = dialogue_index.context_for(*row.identity)
            combine = getattr(context, "name", None) == "SCENE_SUBTITLE"
        text = row.text + "<br>" + other.text if combine and other else row.text
        lines.append(row.prefix + text + row.newline)
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         prefix="merged-", suffix=".csv",
                                         dir=primary.resolve().parent, delete=False) as handle:
            handle.writelines(lines)
            return Path(handle.name)
    except (OSError, UnicodeError) as error:
        raise MergeError(f"Cannot write merged CSV beside {primary}: {error}") from error
