"""Patch the observed Witcher 3 5.00 CRI USM subtitle layout on disk.

The caller owns a verified, seekable temporary source and a separate staged
destination. Validation and patch planning finish before the first write.
Only subtitle cues, directory byte counts and verified absolute seek offsets
may change. Unknown layouts and increases beyond existing subtitle capacities
are refused; callers must discard a destination after any exception.
"""

from __future__ import annotations

import os
import struct
from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .progress import ProgressCallback, report_progress


# Matched against shipped movies/cutscenes/finalboards/subs/fb_1a_<code>.subs.
# All three texts identify each track (two for cz); sidecar spacing and timing
# can differ. hu, tr and ua have sidecars but no established embedded ID.
USM_LOCALE_IDS: dict[str, int] = {
    "en": 0, "pl": 1, "de": 2, "it": 3, "fr": 4, "cz": 5,
    "es": 6, "zh": 7, "ru": 8, "cn": 9, "jp": 10, "kr": 11,
    "br": 12, "esmx": 13, "ar": 14,
}

_BLOCK = 1024 * 1024
_MAX_METADATA = 4 * _BLOCK
_MAX_CUE = 64 * 1024
_MAX_CUES = 65536
_MAX_RETAINED = 32 * _BLOCK
_KINDS = {b"@SFV", b"@SFA", b"@SBT"}
_CUE_BREAK = "\r"
_MARKERS = {
    b"#HEADER END     ===============\0",
    b"#METADATA END   ===============\0",
    b"#CONTENTS END   ===============\0",
}


class USMReadError(ValueError):
    """A USM is malformed or cannot be safely rewritten by this writer."""


@dataclass(frozen=True)
class USMCue:
    locale_id: int
    start: int
    end: int
    text: str
    chunk_offset: int


@dataclass(frozen=True)
class USMMergeResult:
    changed: bool
    primary_count: int
    secondary_count: int
    matched_count: int
    unmatched_primary: tuple[USMCue, ...]
    unmatched_secondary: tuple[USMCue, ...]
    bytes_written: int


@dataclass(frozen=True)
class _Chunk:
    offset: int
    size: int
    padding: int
    header: bytes

    @property
    def key(self) -> tuple[bytes, int]:
        return self.header[:4], self.header[12]

    @property
    def kind(self) -> int:
        return self.header[15]

    @property
    def payload_size(self) -> int:
        return self.size - 32 - self.padding


@dataclass(frozen=True)
class _Cell:
    value: int
    offset: int  # Absolute source offset; table edits never change table size.
    width: int


@dataclass(frozen=True)
class _Table:
    name: str
    columns: tuple[tuple[str, int], ...]
    rows: tuple[dict[str, _Cell], ...]


@dataclass(frozen=True)
class _Subtitle:
    cue: USMCue
    chunk: _Chunk
    fields: bytes
    terminator: bytes
    unit: int
    text_size: int


@dataclass(frozen=True)
class _Edit:
    offset: int
    old_size: int
    data: bytes


def _read(source: BinaryIO, offset: int, size: int) -> bytes:
    """Read bounded metadata/cues or one bounded piece of a streamed copy."""
    if size < 0 or size > _MAX_METADATA:
        raise USMReadError(f"unsupported read size at USM offset {offset}")
    source.seek(offset)
    result = bytearray()
    while len(result) < size:
        part = source.read(min(_BLOCK, size - len(result)))
        if not part:
            raise USMReadError(f"truncated USM at offset {offset + len(result)}")
        result.extend(part)
    return bytes(result)


def _chunks(source: BinaryIO, size: int) -> Iterator[_Chunk]:
    offset = 0
    while offset < size:
        if size - offset < 32:
            raise USMReadError(f"trailing or truncated USM chunk at {offset}")
        h = _read(source, offset, 32)
        length = struct.unpack_from(">I", h, 4)[0] + 8
        padding = struct.unpack_from(">H", h, 10)[0]
        if length < 32 or length % 32 or length > size - offset:
            raise USMReadError(f"invalid USM chunk length at {offset}")
        if h[8:10] != b"\0\x18" or h[13:15] != b"\0\0" or h[24:] != bytes(8):
            raise USMReadError(f"unrecognized USM chunk header at {offset}")
        if padding > length - 32 or h[15] not in (0, 1, 2, 3):
            raise USMReadError(f"invalid USM payload/padding bounds at {offset}")
        if offset == 0:
            # CRI uses this byte as the directory stream channel in some
            # shipped movies (the game-start recap uses channel 12). The
            # directory table and its stream rows are validated below.
            if h[:4] != b"CRID" or h[15] != 1:
                raise USMReadError("USM must start with a CRID directory")
        elif h[:4] not in _KINDS:
            raise USMReadError(f"unsupported USM chunk {h[:4]!r} at {offset}")
        yield _Chunk(offset, length, padding, h)
        offset += length


def _payload(source: BinaryIO, chunk: _Chunk) -> bytes:
    data = _read(source, chunk.offset + 32, chunk.payload_size)
    if any(_read(source, chunk.offset + chunk.size - chunk.padding, chunk.padding)):
        raise USMReadError(f"nonzero metadata/subtitle padding at {chunk.offset}")
    return data


def _table(source: BinaryIO, chunk: _Chunk) -> _Table:
    data = _payload(source, chunk)
    if len(data) < 32 or data[:4] != b"@UTF":
        raise USMReadError(f"expected plaintext @UTF metadata at {chunk.offset}")
    table_size, rows_at, strings_at, blobs_at, name_at, count, stride, row_count = (
        struct.unpack_from(">IIIIIHHI", data, 4)
    )
    rows_at += 8
    strings_at += 8
    blobs_at += 8
    if (table_size + 8 != len(data) or not 32 <= rows_at <= strings_at <= blobs_at <= len(data)
            or count == 0 or count > 64 or not 0 < row_count <= _MAX_CUES
            or count * row_count > 262144
            or rows_at + stride * row_count != strings_at or any(data[blobs_at:])):
        raise USMReadError(f"unsupported @UTF bounds/blob layout at {chunk.offset}")

    def string(at: int) -> str:
        start = strings_at + at
        if not strings_at <= start < blobs_at:
            raise USMReadError(f"invalid @UTF string reference at {chunk.offset}")
        end = data.find(b"\0", start, blobs_at)
        if end < 0:
            raise USMReadError(f"unterminated @UTF string at {chunk.offset}")
        try:
            return data[start:end].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise USMReadError(f"invalid @UTF string encoding at {chunk.offset}") from exc

    columns: list[tuple[str, int]] = []
    rows: list[dict[str, _Cell]] = [{} for _ in range(row_count)]
    cursor, row_cursor = 32, 0
    for _ in range(count):
        if cursor + 5 > rows_at:
            raise USMReadError(f"truncated @UTF column at {chunk.offset}")
        flags, name_offset = struct.unpack_from(">BI", data, cursor)
        cursor += 5
        name = string(name_offset)
        storage, value_type = flags & 0xF0, flags & 0x0F
        width = {0: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 8, 10: 4}.get(value_type)
        if width is None or storage not in (0x30, 0x50) or name in rows[0]:
            raise USMReadError(f"unsupported @UTF column {name!r} at {chunk.offset}")
        if storage == 0x30 and cursor + width > rows_at:
            raise USMReadError(f"truncated @UTF constant at {chunk.offset}")
        if storage == 0x50 and row_cursor + width > stride:
            raise USMReadError(f"@UTF row exceeds stride at {chunk.offset}")
        columns.append((name, flags))
        for index, row in enumerate(rows):
            at = cursor if storage == 0x30 else rows_at + index * stride + row_cursor
            value = int.from_bytes(data[at:at + width], "big", signed=value_type in (3, 5))
            if value_type == 10:
                string(value)  # Validate even strings that do not affect patching.
            row[name] = _Cell(value, chunk.offset + 32 + at, width)
        if storage == 0x30:
            cursor += width
        else:
            row_cursor += width
    if cursor != rows_at or row_cursor != stride:
        raise USMReadError(f"unrecognized @UTF column/row padding at {chunk.offset}")
    return _Table(string(name_at), tuple(columns), tuple(rows))


_VIDEO_COLUMNS = tuple((name, 0x54) for name in (
    "width", "height", "mat_width", "mat_height", "disp_width", "disp_height", "scrn_width"
)) + (("mpeg_dcprec", 0x50), ("mpeg_codec", 0x50))
_VIDEO_TAIL = tuple((name, 0x54) for name in (
    "alpha_type", "total_frames", "framerate_n", "framerate_d", "metadata_count",
    "metadata_size", "ixsize", "pre_padding", "max_picture_size", "color_space", "picture_type"
))
_AUDIO_COLUMNS = (("audio_codec", 0x50), ("sampling_rate", 0x54),
                  ("total_samples", 0x54), ("num_channels", 0x50),
                  ("metadata_count", 0x54), ("metadata_size", 0x54), ("ixsize", 0x54))
_SUBTITLE_COLUMNS = (("time_unit", 0x54), ("total_time", 0x54),
                     ("num_channels", 0x50), ("content_xsize", 0x54), ("ixsize", 0x54))
_SEEK_COLUMNS = (("ofs_byte", 0x56), ("ofs_frmid", 0x55),
                 ("num_skip", 0x33), ("resv", 0x33))


def _check_schema(chunk: _Chunk, table: _Table) -> None:
    kind = chunk.key[0]
    if kind == b"CRID":
        expected = (("fmtver", 0x34), ("filename", 0x5A), ("filesize", 0x54),
                    ("datasize", 0x34), ("stmid", 0x54), ("chno", 0x52),
                    ("minchk", 0x52), ("minbuf", 0x54), ("avbps", 0x54))
        alternate = tuple((key, 0x54 if key in ("fmtver", "datasize") else flag)
                          for key, flag in expected)
        no_fmtver = (("filename", 0x5A), ("filesize", 0x54), ("datasize", 0x54),
                     ("stmid", 0x54), ("chno", 0x52), ("minchk", 0x52),
                     ("minbuf", 0x54), ("avbps", 0x54))
        valid = table.name == "CRIUSF_DIR_STREAM" and table.columns in (
            expected, alternate, no_fmtver,
        )
        valid = valid and all(
            row["datasize"].value == 0
            and ("fmtver" not in row or row["fmtver"].value == 0)
            for row in table.rows
        )
    elif kind == b"@SFV" and chunk.kind == 3:
        row_seek_columns = (
            ("ofs_byte", 0x56), ("ofs_frmid", 0x55),
            ("num_skip", 0x53), ("resv", 0x53),
        )
        valid = (table.name == "VIDEO_SEEKINFO"
                 and table.columns in (_SEEK_COLUMNS, row_seek_columns))
        valid = valid and all(row["num_skip"].value == row["resv"].value == 0
                              and row["ofs_frmid"].value >= 0 for row in table.rows)
    elif kind == b"@SFV" and chunk.kind == 1:
        profile_columns = (("mpeg_profile", 0x50), ("mpeg_level", 0x50))
        valid = table.name == "VIDEO_HDRINFO" and table.columns in (
            _VIDEO_COLUMNS + _VIDEO_TAIL,
            _VIDEO_COLUMNS + _VIDEO_TAIL[:7],
            _VIDEO_COLUMNS + profile_columns + _VIDEO_TAIL,
        )
    elif kind == b"@SFA" and chunk.kind == 1:
        valid = table.name == "AUDIO_HDRINFO" and table.columns in (
            _AUDIO_COLUMNS, _AUDIO_COLUMNS + (("ambisonics", 0x50),))
    elif kind == b"@SBT" and chunk.kind == 1:
        valid = table.name == "SUBTITLE_HDRINFO" and table.columns == _SUBTITLE_COLUMNS
    else:
        valid = False
    if not valid or (chunk.kind == 1 and kind != b"CRID" and len(table.rows) != 1):
        raise USMReadError(f"unsupported {table.name} metadata schema at {chunk.offset}")


def _subtitle(source: BinaryIO, chunk: _Chunk) -> _Subtitle:
    if chunk.payload_size < 20 or chunk.payload_size > _MAX_CUE or chunk.padding >= 32:
        raise USMReadError(f"unsupported subtitle length/padding at {chunk.offset}")
    data = _payload(source, chunk)
    locale, unit, start, duration, text_size = struct.unpack_from("<5I", data)
    if (unit == 0 or duration == 0 or start + duration > 0xFFFFFFFF
            or text_size != len(data) - 20
            or struct.unpack_from(">II", chunk.header, 16) != (start, unit)):
        raise USMReadError(f"invalid subtitle timing/text bounds at {chunk.offset}")
    encoded = data[20:]
    terminator = b"\0\0" if encoded.endswith(b"\0\0") else b""
    if terminator:
        encoded = encoded[:-2]
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise USMReadError(f"non-UTF-8 subtitle at {chunk.offset}") from exc
    if "\n" in text or "\0" in text:
        raise USMReadError(f"unrecognized subtitle line break/terminator at {chunk.offset}")
    return _Subtitle(USMCue(locale, start, start + duration, text, chunk.offset),
                     chunk, data[:20], terminator, unit, text_size)


def _number_edit(cell: _Cell, value: int) -> _Edit:
    if not 0 <= value < 1 << (8 * cell.width):
        raise USMReadError("patched metadata exceeds its integer field width")
    return _Edit(cell.offset, cell.width, value.to_bytes(cell.width, "big"))


def _write(destination: BinaryIO, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        accepted = destination.write(remaining)
        if accepted is None or not 0 < accepted <= len(remaining):
            raise OSError("USM destination did not accept output bytes")
        remaining = remaining[accepted:]


def patch_usm_stream(
    source: BinaryIO,
    destination: BinaryIO,
    primary_locale_id: int,
    secondary_locale_id: int,
    progress_callback: ProgressCallback | None = None,
) -> USMMergeResult:
    """Merge exact cue times; write a complete USM only when text changes.

    An unchanged result writes zero bytes, including when a requested track is
    absent. Duplicate times pair in source order, one-to-one. The source is
    read from offset zero regardless of its current position, stays open, and
    must remain unchanged throughout the call. The destination stays open;
    the caller is responsible for publishing or removing its staged file.
    """
    if (type(primary_locale_id) is not int or type(secondary_locale_id) is not int
            or primary_locale_id not in USM_LOCALE_IDS.values()
            or secondary_locale_id not in USM_LOCALE_IDS.values()
            or primary_locale_id == secondary_locale_id):
        raise USMReadError("USM merge requires two different, confirmed locale IDs")
    if source is destination or not source.seekable() or not source.readable() or not destination.writable():
        raise USMReadError("USM merge requires a seekable source and separate writable destination")
    try:
        source_stat = os.fstat(source.fileno())
        destination_stat = os.fstat(destination.fileno())
    except (AttributeError, OSError):
        pass  # BytesIO and other caller-owned binary streams have no fileno.
    else:
        if (source_stat.st_dev, source_stat.st_ino) == (destination_stat.st_dev, destination_stat.st_ino):
            raise USMReadError("USM source and destination refer to the same file")
    source.seek(0, os.SEEK_END)
    source_size = source.tell()
    if source_size < 32:
        raise USMReadError("empty or truncated USM")

    directory: _Table | None = None
    headers: dict[tuple[bytes, int], _Table] = {}
    metadata_sizes: dict[tuple[bytes, int], list[int]] = defaultdict(list)
    totals: dict[tuple[bytes, int], int] = defaultdict(int)
    seeks: list[tuple[tuple[bytes, int], _Cell]] = []
    pending_seeks: dict[int, tuple[bytes, int]] = {}
    subtitles: list[_Subtitle] = []
    retained = 0
    in_data = False
    units: set[int] = set()
    chunk_count = 0
    for chunk in _chunks(source, source_size):
        chunk_count += 1
        key = chunk.key
        if chunk.kind in (1, 3):
            if in_data:
                raise USMReadError("metadata after media data is unsupported")
            table = _table(source, chunk)
            retained += chunk.payload_size
            if retained > _MAX_RETAINED:
                raise USMReadError("USM metadata/subtitles exceed the supported memory budget")
            _check_schema(chunk, table)
            if key[0] == b"CRID":
                directory = table
            elif chunk.kind == 1:
                if key in headers:
                    raise USMReadError(f"duplicate stream header {key!r}")
                headers[key] = table
            else:
                if key not in headers:
                    raise USMReadError("seek metadata precedes its stream header")
                metadata_sizes[key].append(chunk.size)
                for row in table.rows:
                    cell = row["ofs_byte"]
                    if cell.value <= chunk.offset or cell.value >= source_size or cell.value in pending_seeks:
                        raise USMReadError("invalid or duplicate video seek offset")
                    seeks.append((key, cell))
                    pending_seeks[cell.value] = key
        elif chunk.kind == 2:
            if key not in headers or _payload(source, chunk) not in _MARKERS:
                raise USMReadError(f"unrecognized USM control record at {chunk.offset}")
        else:
            in_data = True
            if key not in headers:
                raise USMReadError(f"USM payload has no stream header at {chunk.offset}")
            if chunk.offset in pending_seeks:
                if pending_seeks.pop(chunk.offset) != key:
                    raise USMReadError("video seek offset targets a different stream")
            totals[key] += chunk.payload_size
            if key[0] == b"@SBT":
                item = _subtitle(source, chunk)
                units.add(item.unit)
                subtitles.append(item)
                retained += chunk.payload_size
                if len(subtitles) > _MAX_CUES or retained > _MAX_RETAINED:
                    raise USMReadError("USM metadata/subtitles exceed the supported memory budget")
        if chunk_count % 256 == 0:
            report_progress(progress_callback, "usm-scan-chunks", chunk_count, None)
    report_progress(progress_callback, "usm-scan-chunks", chunk_count, None)
    if directory is None or pending_seeks or len(units) > 1:
        raise USMReadError("missing directory, invalid seek target, or mixed subtitle time units")

    stream_rows: dict[tuple[bytes, int], dict[str, _Cell]] = {}
    container_row: dict[str, _Cell] | None = None
    for row in directory.rows:
        stream_id, channel = row["stmid"].value, row["chno"].value
        if stream_id == 0 and channel == 0xFFFF:
            if container_row is not None or row["filesize"].value != source_size:
                raise USMReadError("invalid USM container size/directory entry")
            container_row = row
        else:
            key = (stream_id.to_bytes(4, "big"), channel)
            if key not in headers or key in stream_rows or row["filesize"].value != totals[key]:
                raise USMReadError("USM stream directory does not match payload bytes")
            stream_rows[key] = row
    if container_row is None or set(stream_rows) != set(headers):
        raise USMReadError("USM directory and stream headers disagree")
    for key, table in headers.items():
        row = table.rows[0]
        if "metadata_count" in row:
            sizes = metadata_sizes[key]
            if row["metadata_count"].value != len(sizes) or row["metadata_size"].value != sum(sizes):
                raise USMReadError("USM metadata count/size does not match observed chunks")
    subtitle_keys = [key for key in headers if key[0] == b"@SBT"]
    if len(subtitle_keys) > 1:
        raise USMReadError("multiple physical subtitle streams are unsupported")
    if subtitle_keys:
        sub_header = headers[subtitle_keys[0]].rows[0]
        # These are observed timing profiles, not a guessed unit conversion:
        # the 5.00 subtitle header uses 30, whereas individual cues use either
        # 1000 (base-game movies) or 10 (the DLC teleport movie).
        header_unit = sub_header["time_unit"].value
        if header_unit != 30:
            raise USMReadError(f"unsupported subtitle header time_unit {header_unit}; expected 30")
        for item in subtitles:
            if (header_unit, item.unit) not in ((30, 1000), (30, 10)):
                raise USMReadError(
                    f"unsupported subtitle header/cue time units {header_unit}/{item.unit} "
                    f"at {item.chunk.offset}"
                )
        # Preserve total_time verbatim. Shipped files use both zero and
        # nonzero values (recap_wip declares 6 while its cues extend to about
        # 190 seconds), so it is not a reliable cue-range bound. Individual
        # cue timing, locale IDs, and both text/chunk capacities are validated
        # below; none of these edits change subtitle timing or this field.
    if subtitles:
        if (max(item.text_size for item in subtitles) != sub_header["content_xsize"].value
                or any(item.cue.locale_id >= sub_header["num_channels"].value for item in subtitles)
                or max(item.chunk.size for item in subtitles) > sub_header["ixsize"].value):
            raise USMReadError("subtitle header capacities do not match cue data")

    primary = [item for item in subtitles if item.cue.locale_id == primary_locale_id]
    secondary = [item for item in subtitles if item.cue.locale_id == secondary_locale_id]
    available: dict[tuple[int, int], deque[_Subtitle]] = defaultdict(deque)
    for item in secondary:
        available[(item.cue.start, item.cue.end)].append(item)
    matched_secondary: set[int] = set()
    unmatched_primary: list[USMCue] = []
    edits: list[_Edit] = []
    payload_growth = matched = 0
    for item in primary:
        choices = available[(item.cue.start, item.cue.end)]
        if not choices:
            unmatched_primary.append(item.cue)
            continue
        partner = choices.popleft()
        matched_secondary.add(partner.cue.chunk_offset)
        matched += 1
        if not partner.cue.text:
            continue
        encoded = (item.cue.text + _CUE_BREAK + partner.cue.text).encode("utf-8") + item.terminator
        padding = (-(52 + len(encoded))) % 32
        length = 52 + len(encoded) + padding
        # content_xsize and ixsize are verified existing text/chunk capacities.
        # Preserve them and all avbps/minbuf rate estimates: this writer does
        # not infer allocation or bitrate formulas from a few sample values.
        if (len(encoded) > sub_header["content_xsize"].value
                or length > sub_header["ixsize"].value or len(encoded) + 20 > _MAX_CUE):
            raise USMReadError(f"merged cue at {item.chunk.offset} exceeds verified subtitle capacities")
        header = bytearray(item.chunk.header)
        struct.pack_into(">I", header, 4, length - 8)
        struct.pack_into(">H", header, 10, padding)
        fields = bytearray(item.fields)
        struct.pack_into("<I", fields, 16, len(encoded))
        data = bytes(header) + bytes(fields) + encoded + bytes(padding)
        edits.append(_Edit(item.chunk.offset, item.chunk.size, data))
        payload_growth += len(encoded) - item.text_size

    unmatched_secondary = tuple(item.cue for item in secondary
                                if item.cue.chunk_offset not in matched_secondary)
    if not edits:
        return USMMergeResult(False, len(primary), len(secondary), matched,
                              tuple(unmatched_primary), unmatched_secondary, 0)

    # Seek offsets refer to absolute starts of actual video data chunks. Their
    # values are validated against the complete chunk walk before any write.
    positions = [edit.offset for edit in edits]
    growth = [0]
    for edit in edits:
        growth.append(growth[-1] + len(edit.data) - edit.old_size)
    output_size = source_size + growth[-1]
    edits.append(_number_edit(container_row["filesize"], output_size))
    subtitle_row = stream_rows[subtitle_keys[0]]
    edits.append(_number_edit(subtitle_row["filesize"], subtitle_row["filesize"].value + payload_growth))
    for _, cell in seeks:
        shift = growth[bisect_left(positions, cell.value)]
        if shift:
            edits.append(_number_edit(cell, cell.value + shift))
    edits.sort(key=lambda edit: edit.offset)
    previous_end = 0
    for edit in edits:
        if edit.offset < previous_end or edit.offset + edit.old_size > source_size:
            raise USMReadError("overlapping or out-of-bounds USM patch plan")
        previous_end = edit.offset + edit.old_size

    written = 0

    def emit(data: bytes) -> None:
        nonlocal written
        _write(destination, data)
        written += len(data)
        report_progress(progress_callback, "usm-write", written, output_size)

    cursor = 0
    for edit in edits + [_Edit(source_size, 0, b"")]:
        while cursor < edit.offset:
            piece = _read(source, cursor, min(_BLOCK, edit.offset - cursor))
            emit(piece)
            cursor += len(piece)
        if edit.data:
            emit(edit.data)
        cursor = edit.offset + edit.old_size
    return USMMergeResult(True, len(primary), len(secondary), matched,
                          tuple(unmatched_primary), unmatched_secondary, written)
