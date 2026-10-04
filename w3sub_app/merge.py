"""Preserve converter CSV metadata and merge records by localization identity."""
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Protocol

from .dialogue_index import DialogContext
from .models import GameInstallation, MergeMode
from .w3strings_native import StringsFile

_DIALOGUE_SEPARATOR = "<br>"
_NON_DIALOGUE_SEPARATOR = " "
_WESTERN_TERMINAL_PUNCTUATION = ".?!,"
_EASTERN_TERMINAL_PUNCTUATION = {
    "zh": "，。？！",
    "cn": "，。？！",
    "ja": "、，。？！",
    "jp": "、，。？！",
    "ko": ".?!,，。？！",
    "kr": ".?!,，。？！",
    "ar": ".?!,،؛؟۔",
    "fa": ".?!,،؛؟۔",
    "ur": ".?!,،؛؟۔",
}
_CLOSING_QUOTE_MARKS = "\"'”’」』〉》»›"


@dataclass
class MergeStats:
    """Counts primary entries processed and those receiving secondary text."""

    total_entries: int = 0
    merged_entries: int = 0


@dataclass(frozen=True)
class UnmatchedEntry:
    side: str
    string_id: str
    key_hash_hex: str
    text: str
    reason: str


def _language_code(language: str | None) -> str | None:
    if not isinstance(language, str) or not language.strip():
        return None
    return language.strip().casefold().replace("_", "-").split("-", 1)[0]


def _ends_with_terminal_punctuation(text: str, language: str) -> bool:
    """Ignore trailing whitespace and closing quotes before checking punctuation."""
    candidate = text.rstrip()
    while candidate and candidate[-1] in _CLOSING_QUOTE_MARKS:
        candidate = candidate[:-1].rstrip()
    code = _language_code(language)
    punctuation = _EASTERN_TERMINAL_PUNCTUATION.get(
        code, _WESTERN_TERMINAL_PUNCTUATION,
    )
    return candidate.endswith(tuple(punctuation))


def _merge_separator(primary_text: str, secondary_text: str,
                     primary_language: str | None, secondary_language: str | None,
                     *, keyless: bool) -> str:
    """Use Western text for mixed-script pairs; otherwise use the primary text."""
    primary_code = _language_code(primary_language)
    secondary_code = _language_code(secondary_language)
    if primary_code is None or secondary_code is None:
        # Keep the older direct-call behavior for clients that do not provide
        # locale codes. Generation passes both selected languages explicitly.
        return _DIALOGUE_SEPARATOR if keyless else _NON_DIALOGUE_SEPARATOR

    primary_is_eastern = primary_code in _EASTERN_TERMINAL_PUNCTUATION
    secondary_is_eastern = secondary_code in _EASTERN_TERMINAL_PUNCTUATION
    if primary_is_eastern and not secondary_is_eastern:
        sentence, language = secondary_text, secondary_code
    elif secondary_is_eastern and not primary_is_eastern:
        sentence, language = primary_text, primary_code
    else:
        # For two Western or two Eastern languages, follow the primary text.
        sentence, language = primary_text, primary_code
    return (_DIALOGUE_SEPARATOR if _ends_with_terminal_punctuation(sentence, language)
            else _NON_DIALOGUE_SEPARATOR)


def merge_records(primary: StringsFile, secondary: StringsFile, mode: MergeMode,
                  dialogue_index=None, current_game=None,
                  stats: MergeStats | None = None,
                  primary_language: str | None = None,
                  secondary_language: str | None = None) -> StringsFile:
    """Merge hashless dialogue IDs and keyed text by localization identity."""
    if not isinstance(mode, MergeMode):
        raise MergeError(f"Unsupported merge mode: {mode}")
    primary_keys = {}
    secondary_keys = {}
    for key_hash, string_id in primary.keys:
        primary_keys.setdefault(string_id, set()).add(key_hash)
    for key_hash, string_id in secondary.keys:
        secondary_keys.setdefault(string_id, set()).add(key_hash)
    other_texts = dict(secondary.strings)
    strings = []
    for string_id, text in primary.strings:
        if stats is not None:
            stats.total_entries += 1
        keys = primary_keys.get(string_id, set())
        other_keys = secondary_keys.get(string_id, set())
        shared = keys & other_keys
        # Hashless rows are treated as dialogue and match by ID. When hashes
        # exist, use the shared hash and keep those records out of Dialogue-only.
        keyless = not keys and not other_keys
        combine = (string_id in other_texts and keyless
                   if mode is MergeMode.DIALOGUE_ONLY
                   else string_id in other_texts and (bool(shared) or keyless))
        if combine and stats is not None:
            stats.merged_entries += 1
        separator = _merge_separator(
            text, other_texts.get(string_id, ""), primary_language,
            secondary_language, keyless=keyless,
        )
        strings.append((string_id, text + separator + other_texts[string_id]
                        if combine else text))
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

    def context_for_id(self, string_id: str) -> DialogContext:
        ...


@dataclass(frozen=True)
class _Record:
    prefix: str
    identity: tuple[str, str]
    text: str
    newline: str


def _unmatched_from_maps(primary: dict[tuple[str, str | None], str],
                         secondary: dict[tuple[str, str | None], str]
                         ) -> list[UnmatchedEntry]:
    results = []
    for side, current, other in (
            ("primary", primary, secondary), ("secondary", secondary, primary)):
        other_ids = {string_id for string_id, _key_hash in other}
        other_hashes: dict[str, set[str | None]] = {}
        for string_id, key_hash in other:
            other_hashes.setdefault(string_id, set()).add(key_hash)
        for (string_id, key_hash), text in sorted(
                current.items(), key=lambda item: (int(item[0][0]), item[0][1] or "")):
            if (string_id, key_hash) in other:
                continue
            if string_id not in other_ids:
                reason = "string_id_not_found_in_other_language"
            elif key_hash is None and other_hashes.get(string_id):
                reason = "counterpart_has_key_hash"
            elif key_hash is not None and other_hashes.get(string_id) == {None}:
                reason = "counterpart_is_keyless"
            else:
                reason = "no_shared_key_hash"
            results.append(UnmatchedEntry(
                side, string_id, key_hash or "", text, reason,
            ))
    return results


def unmatched_native_entries(primary: StringsFile,
                             secondary: StringsFile) -> list[UnmatchedEntry]:
    """List unmatched string-ID/hash identities from both native files."""
    def entries(record: StringsFile) -> dict[tuple[str, str | None], str]:
        texts = dict(record.strings)
        keys_by_id: dict[int, set[int]] = {}
        for key_hash, string_id in record.keys:
            if string_id in texts:
                keys_by_id.setdefault(string_id, set()).add(key_hash)
        result = {}
        for string_id, text in record.strings:
            hashes = keys_by_id.get(string_id)
            if hashes:
                for key_hash in hashes:
                    result[(str(string_id), format(key_hash, "x"))] = text
            else:
                result[(str(string_id), None)] = text
        return result

    return _unmatched_from_maps(entries(primary), entries(secondary))


def unmatched_csv_entries(primary: Path, secondary: Path) -> list[UnmatchedEntry]:
    """List unmatched string-ID/hash identities from both converter CSV files."""
    def entries(path: Path) -> dict[tuple[str, str | None], str]:
        return {
            (row.identity[0], row.identity[1] or None): row.text
            for row in _read_csv(Path(path)) if isinstance(row, _Record)
        }

    return _unmatched_from_maps(entries(primary), entries(secondary))


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
                        or not re.fullmatch(r"[0-9a-fA-F]*", columns[1].strip())):
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
              current_game: GameInstallation | None = None,
              stats: MergeStats | None = None,
              primary_language: str | None = None,
              secondary_language: str | None = None) -> Path:
    """Create a separate CSV beside primary; callers pair resource paths first."""
    if not isinstance(mode, MergeMode):
        raise MergeError(f"Unsupported merge mode: {mode}")
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
        if stats is not None:
            stats.total_entries += 1
        other = secondary_records.get(row.identity)
        keyless = row.identity[1] == ""
        combine = (other is not None and keyless
                   if mode is MergeMode.DIALOGUE_ONLY
                   else other is not None)
        if combine and stats is not None:
            stats.merged_entries += 1
        separator = _merge_separator(
            row.text, other.text if other is not None else "", primary_language,
            secondary_language, keyless=keyless,
        )
        text = row.text + separator + other.text if combine and other else row.text
        lines.append(row.prefix + text + row.newline)
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         prefix="merged-", suffix=".csv",
                                         dir=primary.resolve().parent, delete=False) as handle:
            handle.writelines(lines)
            return Path(handle.name)
    except (OSError, UnicodeError) as error:
        raise MergeError(f"Cannot write merged CSV beside {primary}: {error}") from error
