"""Build manager-mod movie bundles containing merged sidecars and full videos.

The Witcher 3 5.00 in-game check confirmed that the recap dual subtitle loads
when its modified .subs and complete .usm are together in ``movies.bundle``
with a matching ``metadata.store``. This writer keeps the generated resource
paths and payloads intact; broader movie coverage still depends on each
resource being compatible with the game's movie loader.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import struct
import tempfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .bundle_reader import iter_bundle_entries, write_bundle_entry
from .cutscene_generation import movie_companion_path
from .progress import ProgressCallback, report_progress


BACKEND_ID = "experimental-potato70-v5-metadata-v7"
BACKEND_VERSION = "5"
GAME_ACCEPTANCE_VERIFIED = True
_CHUNK_SIZE = 1024 * 1024
_BUNDLE_NAME = "movies.bundle"


@dataclass(frozen=True)
class MoviePackageGroup:
    source_bundle: str
    mod_directory: str
    bundle_path: str
    metadata_path: str
    resource_paths: tuple[str, ...]


@dataclass(frozen=True)
class MoviePackageBuildResult:
    files: dict[str, Path]
    hashes: dict[str, str]
    groups: tuple[MoviePackageGroup, ...]
    backend_id: str
    backend_version: str


def _depot_path(value: str) -> str:
    value = value.replace("\\", "/")
    parts = value.split("/")
    if (not value or any(not p or p in (".", "..") or p[-1:] in (".", " ") for p in parts)
            or any(ord(c) < 32 or c in ':*?"<>|' for c in value)
            or len(value.encode("utf-8")) > 255):
        raise ValueError(f"Unsafe depot path: {value!r}")
    if any(re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p, re.I) for p in parts):
        raise ValueError(f"Unsafe depot path: {value!r}")
    if Path(value).suffix.casefold() not in (".subs", ".usm"):
        raise ValueError(f"Only movie resources can be packaged: {value!r}")
    return value


def _origin(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("Source bundle identity must be a nonempty path")
    raw = value.replace("/", "\\")
    if raw.endswith("\\"):
        raise ValueError(f"Source bundle identity must name a bundle file: {value!r}")
    _, tail = ntpath.splitdrive(raw)
    if any(part in (".", "..") for part in tail.split("\\")):
        raise ValueError(f"Source bundle identity must not contain traversal: {value!r}")
    if any(any(c in ':*?"<>|' for c in part) for part in tail.split("\\") if part):
        raise ValueError(f"Invalid source bundle identity: {value!r}")
    value = ntpath.normpath(raw).replace("\\", "/").casefold()
    parts = value.split("/")
    if len(parts) < 3 or parts[-2] != "bundles" or not parts[-1].endswith(".bundle"):
        raise ValueError(f"Invalid source bundle identity: {value!r}")
    if not ntpath.isabs(raw) and any(not part for part in parts):
        raise ValueError(f"Invalid source bundle identity: {value!r}")
    return value


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fnv64(value: str) -> int:
    result = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        result = ((result ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return result


def _vlq(value: int) -> bytes:
    # REDengine signed compact integer: sign/continuation/6 bits, then 7 bits.
    if value == 0:
        return b"\x80"
    output = bytearray([value & 63])
    value >>= 6
    if value:
        output[0] |= 64
    while value:
        byte = value & 127
        value >>= 7
        output.append(byte | (128 if value else 0))
    return bytes(output)


def _metadata_bytes(entries, bundle_size: int, bundle_name: str) -> bytes:
    """Encode the movie-only v7 subset observed in independent WCC outputs."""
    strings = bytearray(b"\0" + bundle_name.encode("utf-8") + b"\0")
    names = []
    offsets = []
    for entry in entries:
        name = entry.depot_path.replace("/", "\\")
        names.append(name)
        offsets.append(len(strings))
        strings.extend(name.encode("utf-8") + b"\0")
    directories = {"": 0}
    directory_rows = [(len(strings), 0)]
    strings.append(0)
    file_init = []
    for file_id, (name, offset) in enumerate(zip(names, offsets), 1):
        parent = ""
        for component in name.split("\\")[:-1]:
            directory = f"{parent}\\{component}" if parent else component
            if directory not in directories:
                directories[directory] = len(directory_rows)
                directory_rows.append((len(strings), directories[parent]))
                strings.extend(component.encode("utf-8") + b"\0")
            parent = directory
        basename_offset = offset + len(name.rpartition("\\")[0].encode("utf-8")) + (1 if parent else 0)
        file_init.append((file_id, directories[parent], basename_offset))
    maximum = max(
        (e.uncompressed_size for e in entries if Path(e.depot_path).suffix.casefold() != ".usm"),
        default=0,
    )
    data = bytearray(struct.pack("<4sIII", b"\x03VTM", 7, maximum, maximum))
    data.extend(_vlq(len(strings)) + strings)

    def array(fmt, rows):
        data.extend(_vlq(len(rows)))
        for row in rows:
            data.extend(struct.pack(fmt, *row))

    array("<8I", [(0,) * 8] + [
        (offset, 0, e.compressed_size, e.uncompressed_size, i, 0, 0, 0)
        for i, (e, offset) in enumerate(zip(entries, offsets), 1)
    ])
    array("<Q4I", [(0,) * 5] + [
        (e.offset, e.compressed_size, 0, i, 1) for i, e in enumerate(entries, 1)
    ])
    array("<Q4I", [(0,) * 5, (bundle_size - entries[0].offset, entries[0].offset, 1, 1, len(entries))])
    array("<I", [])  # No buffers in movie resources.
    array("<2I", directory_rows)
    array("<3I", file_init)
    hashes = sorted((_fnv64(name), i) for i, name in enumerate(names, 1))
    # WCC initializes the lower-middle search hint to zero, all other hints to -1.
    array(
        "<QIi",
        [(h, i, 0 if n == (len(hashes) - 1) // 2 else -1) for n, (h, i) in enumerate(hashes)],
    )
    return bytes(data)


class _HashSink:
    def __init__(self):
        self.digest = hashlib.sha256()

    def write(self, data):
        self.digest.update(data)
        return len(data)


class _MetadataCursor:
    """Bounded reader for the REDengine metadata.store compact arrays."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.offset:
            raise ValueError("truncated metadata.store")
        start = self.offset
        self.offset += size
        return self.data[start:self.offset]

    def compact_uint(self) -> int:
        first = self.take(1)[0]
        if first == 0x80:
            return 0
        if first & 0x80:
            raise ValueError("negative metadata compact integer is unsupported")
        value = first & 0x3F
        more = bool(first & 0x40)
        shift = 6
        while more:
            byte = self.take(1)[0]
            value |= (byte & 0x7F) << shift
            more = bool(byte & 0x80)
            shift += 7
            if shift > 63:
                raise ValueError("metadata compact integer is too large")
        return value

    def array(self, row_format: str) -> tuple[tuple[int, ...], ...]:
        count = self.compact_uint()
        row = struct.Struct(row_format)
        if count > (len(self.data) - self.offset) // row.size:
            raise ValueError("metadata array exceeds remaining file bounds")
        result = tuple(row.unpack(self.take(row.size)) for _ in range(count))
        return result

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise ValueError("unexpected trailing bytes in metadata.store")


def _metadata_string(strings: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(strings):
        raise ValueError("metadata string offset is out of bounds")
    end = strings.find(b"\0", offset)
    if end < 0:
        raise ValueError("metadata string is not terminated")
    return strings[offset:end].decode("utf-8")


def _metadata_fnv64(name: str) -> int:
    """Independent metadata reader hash implementation, checked by WCC fixture."""
    value = 0xCBF29CE484222325
    for byte in name.encode("utf-8"):
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _validate_metadata_store(data: bytes, bundle_path: Path, bundle_size: int, entries) -> None:
    if len(data) < 16:
        raise ValueError("metadata.store header is truncated")
    signature, version, maximum_a, maximum_b = struct.unpack_from("<4sIII", data)
    if signature != b"\x03VTM" or version != 7:
        raise ValueError("unsupported metadata.store signature or version")
    largest = max(
        (entry.uncompressed_size for entry in entries if Path(entry.depot_path).suffix.casefold() != ".usm"),
        default=0,
    )
    if maximum_a != largest or maximum_b != largest:
        raise ValueError("metadata.store maximum resource sizes do not match the bundle")

    cursor = _MetadataCursor(data)
    cursor.offset = 16
    strings = cursor.take(cursor.compact_uint())
    expected_bundle_refs = (bundle_path.name, f"bundles\\{bundle_path.name}")
    if not any(
        strings.startswith(b"\0" + reference.encode("utf-8") + b"\0")
        for reference in expected_bundle_refs
    ):
        raise ValueError("metadata.store refers to a different bundle filename")

    file_rows = cursor.array("<8I")
    chunk_rows = cursor.array("<Q4I")
    group_rows = cursor.array("<Q4I")
    buffer_rows = cursor.array("<I")
    directory_rows = cursor.array("<2I")
    file_init_rows = cursor.array("<3I")
    hash_rows = cursor.array("<QIi")
    cursor.finish()

    count = len(entries)
    if len(file_rows) != count + 1 or file_rows[0] != (0,) * 8:
        raise ValueError("metadata.store file table is incomplete")
    if len(chunk_rows) != count + 1 or chunk_rows[0] != (0,) * 5:
        raise ValueError("metadata.store chunk table is incomplete")
    if len(group_rows) != 2 or group_rows[0] != (0,) * 5:
        raise ValueError("metadata.store bundle group table is incomplete")
    if buffer_rows:
        raise ValueError("movie metadata unexpectedly references buffers")
    if len(file_init_rows) != count or len(hash_rows) != count:
        raise ValueError("metadata.store file lookup tables are incomplete")

    names = [entry.depot_path.replace("/", "\\") for entry in entries]
    parent_ids = []
    directory_components: list[tuple[str, int]] = []
    directory_ids = {"": 0}
    for name in names:
        parent = ""
        for component in name.split("\\")[:-1]:
            path = f"{parent}\\{component}" if parent else component
            if path not in directory_ids:
                directory_ids[path] = len(directory_components) + 1
                directory_components.append((component, directory_ids[parent]))
            parent = path
        parent_ids.append(directory_ids[parent])

    if (not directory_rows or _metadata_string(strings, directory_rows[0][0]) != ""
            or directory_rows[0][1] != 0):
        raise ValueError("metadata.store root directory row is invalid")
    if len(directory_rows) != len(directory_components) + 1:
        raise ValueError("metadata.store directory table does not match the bundle paths")
    for row, expected in zip(directory_rows[1:], directory_components):
        offset, parent_id = row
        component, expected_parent = expected
        if parent_id != expected_parent or _metadata_string(strings, offset) != component:
            raise ValueError("metadata.store directory lookup does not match the bundle paths")

    for index, (entry, name, file_row, chunk_row, init_row) in enumerate(
        zip(entries, names, file_rows[1:], chunk_rows[1:], file_init_rows), start=1,
    ):
        string_offset, reserved, compressed, uncompressed, file_id, r0, r1, r2 = file_row
        if (_metadata_string(strings, string_offset) != name or reserved != 0
                or compressed != entry.compressed_size or uncompressed != entry.uncompressed_size
                or file_id != index or (r0, r1, r2) != (0, 0, 0)):
            raise ValueError("metadata.store file row does not match the bundle entry")
        data_offset, chunk_size, chunk_reserved, chunk_file_id, chunk_kind = chunk_row
        if (data_offset, chunk_size, chunk_reserved, chunk_file_id, chunk_kind) != (
            entry.offset, entry.compressed_size, 0, index, 1,
        ):
            raise ValueError("metadata.store chunk row does not match the bundle entry")
        file_id, directory_id, basename_offset = init_row
        if (file_id != index or directory_id != parent_ids[index - 1]
                or _metadata_string(strings, basename_offset) != name.rsplit("\\", 1)[-1]):
            raise ValueError("metadata.store filename lookup does not match the bundle entry")

    first_offset = entries[0].offset
    expected_group = (bundle_size - first_offset, first_offset, 1, 1, count)
    if group_rows[1] != expected_group:
        raise ValueError("metadata.store bundle group does not match the bundle bounds")

    hashes = sorted((_metadata_fnv64(name), index) for index, name in enumerate(names, start=1))
    expected_hash_rows = tuple(
        (name_hash, file_id, 0 if position == (len(hashes) - 1) // 2 else -1)
        for position, (name_hash, file_id) in enumerate(hashes)
    )
    if hash_rows != expected_hash_rows:
        raise ValueError("metadata.store hash lookup table does not match the bundle paths")


def validate_movie_package(
    bundle_path: Path, metadata_path: Path, *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, str]:
    """Validate a complete movie package; return depot-path payload SHA256s.

    Metadata does not contain payload checksums. Payload integrity is checked
    with the bundle CRC; callers retain SHA256s to check an expected generation.
    Same-sized replacement payloads can legitimately share identical metadata.
    """
    bundle_path, metadata_path = Path(bundle_path), Path(metadata_path)
    try:
        entries = tuple(iter_bundle_entries(bundle_path))
        if not entries:
            raise ValueError("Movie package must contain at least one resource")
        names = [_depot_path(e.depot_path) for e in entries]
        if len({p.casefold() for p in names}) != len(names):
            raise ValueError("Colliding depot paths in movie package")
        end = 32 + 304 * len(entries)
        for entry in entries:
            if (entry.compression_method not in (0, 1)
                    or (entry.compression_method == 0
                        and entry.compressed_size != entry.uncompressed_size)):
                raise ValueError("Movie package has unsupported compression metadata")
            if (Path(entry.depot_path).suffix.casefold() == ".subs"
                    and entry.compression_method != 0):
                raise ValueError("Movie subtitle sidecars must remain uncompressed for playback")
            if entry.offset < end:
                raise ValueError("Overlapping movie payloads")
            end = entry.offset + entry.compressed_size
        # Read metadata independently of the writer and cross-check its index
        # against the bundle entries before streaming payload hashes.
        metadata_size = metadata_path.stat().st_size
        if metadata_size > 64 * 1024 * 1024:
            raise ValueError("metadata.store exceeds the 64 MiB validation limit")
        _validate_metadata_store(
            metadata_path.read_bytes(), bundle_path, bundle_path.stat().st_size, entries,
        )
        result = {}
        total = sum(entry.uncompressed_size for entry in entries)
        completed = 0
        report_progress(progress_callback, "Validating movie bundle payloads", 0, total or None)
        for name, entry in zip(names, entries):
            if (Path(name).suffix.casefold() == ".usm"
                    and entry.compression_method != 0):
                raise ValueError("USM movie resources must remain uncompressed for playback")
            sink = _HashSink()
            def relay(update):
                report_progress(
                    progress_callback, "Validating movie bundle payloads",
                    completed + update.completed, total or None,
                )
            write_bundle_entry(bundle_path, entry, sink, relay)
            result[name] = sink.digest.hexdigest()
            completed += entry.uncompressed_size
            report_progress(progress_callback, "Validating movie bundle payloads",
                            completed, total or None)
        return result
    except OSError as exc:
        raise ValueError(f"Incomplete or unreadable movie package: {exc}") from exc
    except (IndexError, struct.error, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid movie package metadata: {exc}") from exc


def build_movie_packages(
    resources: Mapping[str, Path],
    source_bundles: Mapping[str, Sequence[str]],
    output_root: Path,
    *,
    progress_callback: ProgressCallback | None = None,
) -> MoviePackageBuildResult:
    """Build the combined manager movie package into a NEW directory.

    ``source_bundles`` maps each resource key to every equivalent source origin
    and is checked before packaging. All changed resources share one
    ``movies.bundle`` so a changed sidecar and its complete companion movie
    are loaded as one override. Movie payloads and subtitle sidecars stay
    uncompressed to match the shipped movie bundle layout. A failed build
    leaves no partial package in output_root.
    """
    output_root = Path(output_root)
    inputs = {}
    seen = set()
    for raw_path, source in resources.items():
        depot = _depot_path(raw_path)
        if depot.casefold() in seen:
            raise ValueError(f"Colliding depot path: {raw_path!r}")
        seen.add(depot.casefold())
        source = Path(source)
        size = source.stat().st_size
        if not source.is_file() or size > 0xFFFFFFFF:
            raise ValueError(f"Unsupported movie source size or type: {source}")
        inputs[depot] = (source, size)
        origins = source_bundles.get(raw_path)
        if not origins or isinstance(origins, (str, bytes)):
            raise ValueError(f"Missing source bundle provenance: {raw_path}")
        for origin in origins:
            _origin(origin)
    if output_root.exists():
        raise ValueError("Movie output directory must not already exist")
    if not inputs:
        return MoviePackageBuildResult({}, {}, (), BACKEND_ID, BACKEND_VERSION)

    resource_paths = set(inputs)
    for depot in sorted(resource_paths):
        if depot.endswith(".subs"):
            companion = movie_companion_path(depot)
            if companion is None or companion not in resource_paths:
                raise ValueError(f"Movie sidecar has no full .usm companion: {depot}")

    total = sum(inputs[p][1] for p in resource_paths)
    completed = 0
    groups = []
    hashes = {}
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".movie-packages-", dir=output_root.parent) as temp:
        stage = Path(temp) / "output"
        stage.mkdir()
        # One stable mod folder and one movies.bundle ensure that sidecars and
        # full movie files are seen together by the game's override loader.
        for origin, paths in (("combined", resource_paths),):
            mod = "modW3DualSubtitleManager"
            bundle_target = f"Mods/{mod}/content/bundles/{_BUNDLE_NAME}"
            metadata_target = f"Mods/{mod}/content/metadata.store"
            group = MoviePackageGroup(origin, mod, bundle_target, metadata_target, tuple(sorted(paths)))
            bundle = stage / bundle_target
            bundle.parent.mkdir(parents=True)
            names = group.resource_paths
            table_size = 304 * len(names)
            if table_size + 32 > 0xFFFFFFFF:
                raise ValueError("Movie bundle table is too large")
            with bundle.open("w+b") as stream:
                stream.write(b"\0" * 32)
                for _ in names:
                    stream.write(b"\0" * 304)
                for index, depot in enumerate(names):
                    source, size = inputs[depot]
                    stream.write(b"\0" * (-stream.tell() % 16))
                    offset = stream.tell()
                    checksum = 0
                    copied = 0
                    with source.open("rb") as payload:
                        while chunk := payload.read(_CHUNK_SIZE):
                            if copied + len(chunk) > size:
                                raise ValueError(f"Movie source changed while packaging: {source}")
                            stream.write(chunk)
                            checksum = zlib.crc32(chunk, checksum)
                            copied += len(chunk)
                            completed += len(chunk)
                            report_progress(progress_callback, "movie-package-bytes", completed, total or None)
                    if copied != size:
                        raise ValueError(f"Movie source changed while packaging: {source}")
                    end = stream.tell()
                    compressed_size = size
                    compression_method = 0
                    row = bytearray(304)
                    name = depot.replace("/", "\\").encode("utf-8")
                    row[:len(name)] = name
                    struct.pack_into(
                        "<QIIII", row, 272, offset, size, compressed_size,
                        checksum & 0xFFFFFFFF, compression_method,
                    )
                    stream.seek(32 + index * 304)
                    stream.write(row)
                    stream.seek(end)
                # WCC's metadata parser rejects extremely small archives. This
                # scratch-proven minimum avoids that and supplies final alignment.
                final_size = max(4096, (stream.tell() + 15) // 16 * 16)
                stream.write(b"\0" * (final_size - stream.tell()))
                stream.seek(0)
                stream.write(struct.pack("<8sQIHQH", b"POTATO70", final_size, table_size, 5, 32 + table_size, 0))
            entries = tuple(iter_bundle_entries(bundle))
            metadata = stage / metadata_target
            metadata.write_bytes(_metadata_bytes(
                entries, final_size, f"bundles\\{bundle.name}",
            ))
            validate_movie_package(bundle, metadata, progress_callback=progress_callback)
            for target in (bundle_target, metadata_target):
                hashes[target] = _hash_file(stage / target)
            groups.append(group)
        # Publish all groups only after every pair has passed validation.
        os.rename(stage, output_root)
    return MoviePackageBuildResult(
        {target: output_root / target for target in hashes}, hashes, tuple(groups), BACKEND_ID, BACKEND_VERSION,
    )
