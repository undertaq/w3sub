"""Merge CRI Sofdec UTF-16LE ``.subs`` sidecars without changing row layout.

CRI represents a line break *inside* subtitle text with a NUL character.
CRLF remains the delimiter between sidecar rows.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


_BOM = b"\xff\xfe"
_CUE_BREAK = "\x00"


@dataclass(frozen=True)
class SubsCue:
    start: int
    end: int
    text: str
    source_line: int


@dataclass(frozen=True)
class SubsMergeResult:
    data: bytes
    primary_count: int
    secondary_count: int
    matched_count: int
    unmatched_primary: tuple[SubsCue, ...]
    unmatched_secondary: tuple[SubsCue, ...]


@dataclass(frozen=True)
class _Row:
    content: str
    ending: str
    cue: SubsCue | None = None
    cue_prefix: str = ""


@dataclass(frozen=True)
class _Sidecar:
    time_unit: int
    rows: tuple[_Row, ...]


def _parse(data: bytes, label: str) -> _Sidecar:
    if not data.startswith(_BOM):
        raise ValueError(f"{label} .subs file must have a UTF-16LE BOM")
    try:
        decoded = data[len(_BOM):].decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} .subs file is not valid UTF-16LE") from exc

    # CRI's sidecar records use CRLF. A bare line break would make cue text
    # ambiguous, so refuse it instead of silently changing row boundaries.
    without_rows = decoded.replace("\r\n", "")
    if "\r" in without_rows or "\n" in without_rows:
        raise ValueError(f"{label} .subs file contains a bare line break")

    parts = decoded.split("\r\n")
    rows: list[_Row] = []
    time_unit: int | None = None
    for index, content in enumerate(parts):
        if index == len(parts) - 1 and not content:
            break  # The final CRLF belongs to the preceding row.
        ending = "\r\n" if index < len(parts) - 1 else ""
        line_number = index + 1
        stripped = content.strip()
        if not stripped or content.lstrip().startswith(";"):
            rows.append(_Row(content, ending))
            continue

        if time_unit is None:
            try:
                time_unit = int(stripped)
            except ValueError as exc:
                raise ValueError(f"{label} .subs line {line_number}: invalid time unit") from exc
            if time_unit <= 0:
                raise ValueError(f"{label} .subs line {line_number}: time unit must be positive")
            rows.append(_Row(content, ending))
            continue

        fields = content.split(",", 2)
        if len(fields) != 3:
            if "," in content:
                raise ValueError(f"{label} .subs line {line_number}: malformed cue row")
            rows.append(_Row(content, ending))
            continue
        try:
            start = int(fields[0].strip())
            end = int(fields[1].strip())
        except ValueError as exc:
            raise ValueError(f"{label} .subs line {line_number}: invalid cue time") from exc
        if start < 0 or end <= start:
            raise ValueError(f"{label} .subs line {line_number}: invalid cue interval")
        cue = SubsCue(start, end, fields[2], line_number)
        rows.append(_Row(content, ending, cue, fields[0] + "," + fields[1] + ","))

    if time_unit is None:
        raise ValueError(f"{label} .subs file has no time-unit header")
    return _Sidecar(time_unit, tuple(rows))


def merge_subs(primary_data: bytes, secondary_data: bytes) -> SubsMergeResult:
    """Append secondary cue text at exact matching times in the primary file.

    Duplicate timing keys are paired in source order, one-to-one. Unpaired
    duplicates appear in the corresponding unmatched list. A mismatched time
    unit is rejected because equal integers would describe different times.
    """
    primary = _parse(primary_data, "primary")
    secondary = _parse(secondary_data, "secondary")
    if primary.time_unit != secondary.time_unit:
        raise ValueError(".subs time units differ")

    available: dict[tuple[int, int], deque[SubsCue]] = defaultdict(deque)
    secondary_cues: list[SubsCue] = []
    for row in secondary.rows:
        if row.cue is not None:
            secondary_cues.append(row.cue)
            available[(row.cue.start, row.cue.end)].append(row.cue)

    output: list[str] = []
    primary_count = matched_count = 0
    unmatched_primary: list[SubsCue] = []
    matched_secondary_lines: set[int] = set()
    for row in primary.rows:
        cue = row.cue
        content = row.content
        if cue is not None:
            primary_count += 1
            choices = available[(cue.start, cue.end)]
            if choices:
                partner = choices.popleft()
                matched_secondary_lines.add(partner.source_line)
                matched_count += 1
                if partner.text:
                    content = row.cue_prefix + cue.text + _CUE_BREAK + partner.text
            else:
                unmatched_primary.append(cue)
        output.append(content + row.ending)

    unmatched_secondary = tuple(
        cue for cue in secondary_cues if cue.source_line not in matched_secondary_lines
    )
    merged = _BOM + "".join(output).encode("utf-16-le", errors="strict")
    return SubsMergeResult(
        merged,
        primary_count,
        len(secondary_cues),
        matched_count,
        tuple(unmatched_primary),
        unmatched_secondary,
    )
