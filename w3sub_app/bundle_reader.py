"""Read-only inventory and extraction for Remastered POTATO70 v5 bundles."""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from .progress import ProgressCallback, report_progress


_HEADER_SIZE = 32
_ENTRY_SIZE = 304
_SIGNATURE = b"POTATO70"
_READ_CHUNK_SIZE = 1024 * 1024


class BundleReadError(ValueError):
    """A bundle is unsupported, malformed, or failed payload verification."""

    def __init__(self, bundle_path: Path, reason: str) -> None:
        self.bundle_path = Path(bundle_path)
        self.reason = reason
        super().__init__(f"{self.bundle_path}: {reason}")


@dataclass(frozen=True)
class BundleEntry:
    bundle_path: Path
    entry_index: int
    depot_path: str
    offset: int
    compressed_size: int
    uncompressed_size: int
    crc32: int
    compression_method: int


def enumerate_witcher_bundles(game_root: Path) -> tuple[Path, ...]:
    """Return sorted active bundles under content and optional dlc roots."""
    game_root = Path(game_root)
    bundles: list[Path] = []
    for root_name in ("content", "dlc"):
        root = game_root / root_name
        if not root.is_dir():
            continue
        def raise_walk_error(error: OSError) -> None:
            failed_path = Path(error.filename) if error.filename else root
            raise BundleReadError(failed_path, f"cannot enumerate bundle directory: {error}") from error

        for directory, child_dirs, files in os.walk(root, onerror=raise_walk_error):
            child_dirs[:] = sorted(
                (name for name in child_dirs if name.casefold() != "dlc-tombstones"),
                key=str.casefold,
            )
            for filename in files:
                if filename.casefold().endswith(".bundle"):
                    bundles.append(Path(directory) / filename)
    return tuple(sorted(bundles, key=lambda path: (path.as_posix().casefold(), path.as_posix())))


def _read_exact(stream, size: int, path: Path, what: str) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise BundleReadError(path, f"truncated {what}: expected {size} bytes, got {len(data)}")
    return data


def iter_bundle_entries(bundle_path: Path) -> Iterator[BundleEntry]:
    """Yield each physical v5 entry, preserving duplicate depot paths."""
    path = Path(bundle_path)
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise BundleReadError(path, f"cannot open bundle: {exc}") from exc

    with stream:
        try:
            physical_size = path.stat().st_size
            header = _read_exact(stream, _HEADER_SIZE, path, "bundle header")
            if header[:8] != _SIGNATURE:
                raise BundleReadError(path, "invalid POTATO70 signature")

            declared_size = struct.unpack_from("<Q", header, 8)[0]
            metadata_size = struct.unpack_from("<I", header, 16)[0]
            version = struct.unpack_from("<H", header, 20)[0]
            if version != 5:
                raise BundleReadError(path, f"unsupported POTATO70 header version {version}; expected v5")
            if declared_size != physical_size:
                raise BundleReadError(
                    path,
                    f"declared bundle size {declared_size} does not match file size {physical_size}",
                )
            if metadata_size % _ENTRY_SIZE:
                raise BundleReadError(
                    path,
                    f"entry metadata size {metadata_size} is not divisible by {_ENTRY_SIZE}",
                )

            entry_count = metadata_size // _ENTRY_SIZE
            table_end = _HEADER_SIZE + metadata_size
            if table_end > physical_size:
                raise BundleReadError(path, "truncated entry metadata table")

            for entry_index in range(entry_count):
                raw = _read_exact(stream, _ENTRY_SIZE, path, f"entry metadata {entry_index}")
                name_bytes = raw[:256].split(b"\0", 1)[0]
                if not name_bytes:
                    raise BundleReadError(path, f"entry {entry_index} has an empty depot path")
                depot_path = name_bytes.decode("utf-8", errors="surrogateescape")
                offset, uncompressed_size, compressed_size, checksum, method = struct.unpack_from(
                    "<QIIIB", raw, 272
                )
                if offset < table_end:
                    raise BundleReadError(path, f"entry {entry_index} payload overlaps metadata")
                if offset > physical_size or compressed_size > physical_size - offset:
                    raise BundleReadError(path, f"entry {entry_index} payload is out of bounds")
                yield BundleEntry(
                    bundle_path=path,
                    entry_index=entry_index,
                    depot_path=depot_path,
                    offset=offset,
                    compressed_size=compressed_size,
                    uncompressed_size=uncompressed_size,
                    crc32=checksum,
                    compression_method=method,
                )
        except OSError as exc:
            raise BundleReadError(path, f"cannot read bundle: {exc}") from exc


def read_bundle_entry(bundle_path: Path, entry: BundleEntry) -> bytes:
    """Read and verify one entry against its current bundle metadata and payload."""
    path = Path(bundle_path)
    if Path(entry.bundle_path).resolve() != path.resolve():
        raise BundleReadError(path, "entry belongs to a different bundle")
    try:
        physical_size = path.stat().st_size
        if entry.offset < 0 or entry.compressed_size < 0 or entry.uncompressed_size < 0:
            raise BundleReadError(path, f"entry {entry.entry_index} has negative bounds or sizes")
        if entry.compression_method not in (0, 1):
            raise BundleReadError(
                path,
                f"entry {entry.entry_index} uses unsupported compression method {entry.compression_method}",
            )

        result = bytearray()
        checksum = 0
        decoder = zlib.decompressobj() if entry.compression_method == 1 else None
        with path.open("rb") as stream:
            header = _read_exact(stream, _HEADER_SIZE, path, "bundle header")
            if header[:8] != _SIGNATURE:
                raise BundleReadError(path, "invalid POTATO70 signature")
            declared_size = struct.unpack_from("<Q", header, 8)[0]
            metadata_size = struct.unpack_from("<I", header, 16)[0]
            version = struct.unpack_from("<H", header, 20)[0]
            if version != 5:
                raise BundleReadError(path, f"unsupported POTATO70 header version {version}; expected v5")
            if declared_size != physical_size:
                raise BundleReadError(
                    path,
                    f"declared bundle size {declared_size} does not match file size {physical_size}",
                )
            if metadata_size % _ENTRY_SIZE:
                raise BundleReadError(
                    path,
                    f"entry metadata size {metadata_size} is not divisible by {_ENTRY_SIZE}",
                )
            table_end = _HEADER_SIZE + metadata_size
            entry_count = metadata_size // _ENTRY_SIZE
            if table_end > physical_size:
                raise BundleReadError(path, "truncated entry metadata table")
            if entry.entry_index < 0 or entry.entry_index >= entry_count:
                raise BundleReadError(path, f"entry index {entry.entry_index} is outside current metadata table")

            stream.seek(_HEADER_SIZE + entry.entry_index * _ENTRY_SIZE)
            raw = _read_exact(stream, _ENTRY_SIZE, path, f"entry metadata {entry.entry_index}")
            name_bytes = raw[:256].split(b"\0", 1)[0]
            current_depot_path = name_bytes.decode("utf-8", errors="surrogateescape")
            current_offset, current_uncompressed_size, current_compressed_size, current_crc32, current_method = (
                struct.unpack_from("<QIIIB", raw, 272)
            )
            current_entry = (
                current_depot_path,
                current_offset,
                current_compressed_size,
                current_uncompressed_size,
                current_crc32,
                current_method,
            )
            requested_entry = (
                entry.depot_path,
                entry.offset,
                entry.compressed_size,
                entry.uncompressed_size,
                entry.crc32,
                entry.compression_method,
            )
            if current_entry != requested_entry:
                raise BundleReadError(path, f"entry {entry.entry_index} metadata is stale or does not match bundle")
            if current_offset < table_end:
                raise BundleReadError(path, f"entry {entry.entry_index} payload overlaps metadata")
            if current_offset > physical_size or current_compressed_size > physical_size - current_offset:
                raise BundleReadError(path, f"entry {entry.entry_index} payload is out of bounds")

            stream.seek(entry.offset)
            remaining_compressed = entry.compressed_size
            while remaining_compressed:
                chunk = stream.read(min(_READ_CHUNK_SIZE, remaining_compressed))
                if not chunk:
                    raise BundleReadError(path, f"entry {entry.entry_index} payload is truncated")
                remaining_compressed -= len(chunk)

                if decoder is None:
                    pieces = (chunk,)
                else:
                    pending = chunk
                    while pending:
                        limit = entry.uncompressed_size - len(result) + 1
                        piece = decoder.decompress(pending, max(1, limit))
                        pending = decoder.unconsumed_tail
                        if decoder.unused_data:
                            raise BundleReadError(path, f"entry {entry.entry_index} has trailing zlib data")
                        if pending and not piece:
                            raise BundleReadError(path, f"entry {entry.entry_index} has invalid zlib data")
                        result.extend(piece)
                        if len(result) > entry.uncompressed_size:
                            raise BundleReadError(
                                path, f"entry {entry.entry_index} exceeds declared uncompressed size"
                            )
                        checksum = zlib.crc32(piece, checksum)
                    pieces = ()

                for piece in pieces:
                    result.extend(piece)
                    if len(result) > entry.uncompressed_size:
                        raise BundleReadError(
                            path, f"entry {entry.entry_index} exceeds declared uncompressed size"
                        )
                    checksum = zlib.crc32(piece, checksum)

            if decoder is not None:
                tail = decoder.flush(max(1, entry.uncompressed_size - len(result) + 1))
                result.extend(tail)
                checksum = zlib.crc32(tail, checksum)
                if len(result) > entry.uncompressed_size:
                    raise BundleReadError(
                        path, f"entry {entry.entry_index} exceeds declared uncompressed size"
                    )
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise BundleReadError(path, f"entry {entry.entry_index} has invalid zlib data")

        if len(result) != entry.uncompressed_size:
            raise BundleReadError(
                path,
                f"entry {entry.entry_index} size mismatch: expected {entry.uncompressed_size}, got {len(result)}",
            )
        if checksum & 0xFFFFFFFF != entry.crc32:
            raise BundleReadError(path, f"entry {entry.entry_index} CRC mismatch")
        return bytes(result)
    except BundleReadError:
        raise
    except (OSError, zlib.error) as exc:
        raise BundleReadError(path, f"cannot read entry {entry.entry_index}: {exc}") from exc


def write_bundle_entry(
    bundle_path: Path,
    entry: BundleEntry,
    destination: BinaryIO,
    progress_callback: ProgressCallback | None = None,
) -> int:
    """Stream and verify one entry into a caller-owned binary stream.

    Bytes may have been written when verification fails. Callers should write
    to a temporary file and discard it unless this function returns normally.
    """
    path = Path(bundle_path)
    if Path(entry.bundle_path).resolve() != path.resolve():
        raise BundleReadError(path, "entry belongs to a different bundle")
    try:
        physical_size = path.stat().st_size
        if entry.offset < 0 or entry.compressed_size < 0 or entry.uncompressed_size < 0:
            raise BundleReadError(path, f"entry {entry.entry_index} has negative bounds or sizes")
        if entry.compression_method not in (0, 1):
            raise BundleReadError(
                path,
                f"entry {entry.entry_index} uses unsupported compression method {entry.compression_method}",
            )

        checksum = 0
        written = 0
        decoder = zlib.decompressobj() if entry.compression_method == 1 else None
        with path.open("rb") as stream:
            header = _read_exact(stream, _HEADER_SIZE, path, "bundle header")
            if header[:8] != _SIGNATURE:
                raise BundleReadError(path, "invalid POTATO70 signature")
            declared_size = struct.unpack_from("<Q", header, 8)[0]
            metadata_size = struct.unpack_from("<I", header, 16)[0]
            version = struct.unpack_from("<H", header, 20)[0]
            if version != 5:
                raise BundleReadError(path, f"unsupported POTATO70 header version {version}; expected v5")
            if declared_size != physical_size:
                raise BundleReadError(
                    path,
                    f"declared bundle size {declared_size} does not match file size {physical_size}",
                )
            if metadata_size % _ENTRY_SIZE:
                raise BundleReadError(
                    path,
                    f"entry metadata size {metadata_size} is not divisible by {_ENTRY_SIZE}",
                )
            table_end = _HEADER_SIZE + metadata_size
            entry_count = metadata_size // _ENTRY_SIZE
            if table_end > physical_size:
                raise BundleReadError(path, "truncated entry metadata table")
            if entry.entry_index < 0 or entry.entry_index >= entry_count:
                raise BundleReadError(path, f"entry index {entry.entry_index} is outside current metadata table")

            stream.seek(_HEADER_SIZE + entry.entry_index * _ENTRY_SIZE)
            raw = _read_exact(stream, _ENTRY_SIZE, path, f"entry metadata {entry.entry_index}")
            name_bytes = raw[:256].split(b"\0", 1)[0]
            current_offset, current_uncompressed_size, current_compressed_size, current_crc32, current_method = (
                struct.unpack_from("<QIIIB", raw, 272)
            )
            current_entry = (
                name_bytes.decode("utf-8", errors="surrogateescape"), current_offset,
                current_compressed_size, current_uncompressed_size, current_crc32, current_method,
            )
            requested_entry = (
                entry.depot_path, entry.offset, entry.compressed_size,
                entry.uncompressed_size, entry.crc32, entry.compression_method,
            )
            if current_entry != requested_entry:
                raise BundleReadError(path, f"entry {entry.entry_index} metadata is stale or does not match bundle")
            if current_offset < table_end:
                raise BundleReadError(path, f"entry {entry.entry_index} payload overlaps metadata")
            if current_offset > physical_size or current_compressed_size > physical_size - current_offset:
                raise BundleReadError(path, f"entry {entry.entry_index} payload is out of bounds")

            def emit(piece: bytes) -> None:
                nonlocal written, checksum
                if not piece:
                    return
                if written + len(piece) > entry.uncompressed_size:
                    raise BundleReadError(
                        path, f"entry {entry.entry_index} exceeds declared uncompressed size"
                    )
                view = memoryview(piece)
                while view:
                    count = destination.write(view)
                    if count is None or count <= 0:
                        raise OSError("destination stream did not accept payload bytes")
                    view = view[count:]
                written += len(piece)
                checksum = zlib.crc32(piece, checksum)
                report_progress(progress_callback, "bundle-entry", written, entry.uncompressed_size or None)

            stream.seek(entry.offset)
            remaining_compressed = entry.compressed_size
            while remaining_compressed:
                chunk = stream.read(min(_READ_CHUNK_SIZE, remaining_compressed))
                if not chunk:
                    raise BundleReadError(path, f"entry {entry.entry_index} payload is truncated")
                remaining_compressed -= len(chunk)
                if decoder is None:
                    emit(chunk)
                    continue
                pending = chunk
                while True:
                    piece = decoder.decompress(pending, _READ_CHUNK_SIZE)
                    pending = decoder.unconsumed_tail
                    if decoder.unused_data:
                        raise BundleReadError(path, f"entry {entry.entry_index} has trailing zlib data")
                    if pending and not piece:
                        raise BundleReadError(path, f"entry {entry.entry_index} has invalid zlib data")
                    emit(piece)
                    if not pending and len(piece) < _READ_CHUNK_SIZE:
                        break

            if decoder is not None:
                # Drain output buffered by zlib in fixed-size pieces. Once a
                # short result is returned, no more output is pending.
                while True:
                    tail = decoder.decompress(b"", _READ_CHUNK_SIZE)
                    if decoder.unused_data:
                        raise BundleReadError(path, f"entry {entry.entry_index} has trailing zlib data")
                    emit(tail)
                    if len(tail) < _READ_CHUNK_SIZE:
                        break
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise BundleReadError(path, f"entry {entry.entry_index} has invalid zlib data")

        if written != entry.uncompressed_size:
            raise BundleReadError(
                path,
                f"entry {entry.entry_index} size mismatch: expected {entry.uncompressed_size}, got {written}",
            )
        if checksum & 0xFFFFFFFF != entry.crc32:
            raise BundleReadError(path, f"entry {entry.entry_index} CRC mismatch")
        return written
    except BundleReadError:
        raise
    except (OSError, zlib.error) as exc:
        raise BundleReadError(path, f"cannot read entry {entry.entry_index}: {exc}") from exc
