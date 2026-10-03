"""Preserve converter CSV metadata and merge records by localization identity."""
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Protocol

from .dialogue_index import DialogContext
from .models import GameInstallation, MergeMode
from .w3strings_native import StringsFile


def merge_records(primary: StringsFile, secondary: StringsFile, mode: MergeMode,
                  dialogue_index=None, current_game=None) -> StringsFile:
    """Merge by ID and a real shared hash; retain all primary key references."""
    if not isinstance(mode, MergeMode):
        raise MergeError(f"Unsupported merge mode: {mode}")
    primary_keys = {}
    secondary_keys = {}
    for key_hash, string_id in primary.keys:
        primary_keys.setdefault(string_id, set()).add(key_hash)
    for key_hash, string_id in secondary.keys:
        secondary_keys.setdefault(string_id, set()).add(key_hash)
    other_texts = dict(secondary.strings)
    current = False
    if (mode is MergeMode.DIALOGUE_ONLY and current_game is not None
            and getattr(dialogue_index, "validated", False) is True):
        try:
            current = dialogue_index.is_current(current_game) is True
        except Exception:
            pass
    strings = []
    for string_id, text in primary.strings:
        keys = primary_keys.get(string_id, set())
        shared = keys & secondary_keys.get(string_id, set())
        combine = bool(shared) and string_id in other_texts
        if combine and mode is MergeMode.DIALOGUE_ONLY:
            # One string can serve several contexts; every primary association
            # must be verified as a subtitle before changing its shared text.
            combine = current
            if combine:
                try:
                    combine = all(dialogue_index.context_for(str(string_id), format(key, "x"))
                                  is DialogContext.SCENE_SUBTITLE for key in keys)
                except Exception:
                    combine = False
        strings.append((string_id, text + "<br>" + other_texts[string_id] if combine else text))
    return StringsFile(primary.version, primary.language_key, tuple(strings), primary.keys)


class MergeError(RuntimeError):
    """CSV data or a requested merge policy cannot be safely used."""


class DialogueIndex(Protocol):
    """Task 4's validated index boundary; raw/unvalidated indexes fail closed."""

    validated: bool

    def is_current(self, game: GameInstallation) -> bool:
        ...

    def context_for(self, string_id: str, key: str) -> DialogContext:
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
              dialogue_index: DialogueIndex | None = None,
              current_game: GameInstallation | None = None) -> Path:
    """Create a separate CSV beside primary; callers pair resource paths first."""
    if not isinstance(mode, MergeMode):
        raise MergeError(f"Unsupported merge mode: {mode}")
    index_is_current = False
    if (mode is MergeMode.DIALOGUE_ONLY
            and getattr(dialogue_index, "validated", False) is True
            and callable(getattr(dialogue_index, "context_for", None))
            and current_game is not None):
        is_current = getattr(dialogue_index, "is_current", None)
        if callable(is_current):
            try:
                index_is_current = is_current(current_game) is True
            except Exception:
                # A stale or unreadable index is a primary-only result.
                index_is_current = False
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
            combine = False
            if index_is_current:
                assert dialogue_index is not None
                try:
                    context = dialogue_index.context_for(*row.identity)
                except Exception:
                    context = DialogContext.UNKNOWN
                combine = context is DialogContext.SCENE_SUBTITLE
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
