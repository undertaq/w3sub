"""Standard-library W3Strings codec (162/163 UTF-16LE; 164 strict UTF-8).

Adapted from Witcher3StringEditor, MIT; see third_party/w3strings_codec.
Records deliberately retain every key association instead of collapsing by ID.
"""
from dataclasses import dataclass
from pathlib import Path
import struct

CODEC_VERSION = "native-w3strings-2"
LANGUAGE_MAGICS = {
    0: 0, 0x24987354: 0x21793217, 0x75886138: 0x42791159,
    0x43975139: 0x79321793, 0x18796651: 0x42387566,
    0x23863176: 0x75921975, 0x42378932: 0x67823218,
    0x45931894: 0x12375973, 0x54834893: 0x59825646,
    0x83496237: 0x73946816, 0x63481486: 0x42386347,
    0x18632176: 0x16875467,
}


class NativeCodecError(ValueError):
    """Unsupported or malformed binary container."""


@dataclass(frozen=True)
class StringsFile:
    version: int
    language_key: int
    strings: tuple[tuple[int, str], ...]
    keys: tuple[tuple[int, int], ...]  # (localization-key hash, string ID)


def _uint(value):
    return type(value) is int and 0 <= value <= 0xffffffff


def _validate(record):
    if type(record.version) is not int or record.version not in (162, 163, 164):
        raise NativeCodecError(f"Unsupported container version: {record.version}")
    if not _uint(record.language_key) or record.language_key not in LANGUAGE_MAGICS:
        raise NativeCodecError(f"Unsupported language key: {record.language_key}")
    ids = set()
    for string_id, text in record.strings:
        if not _uint(string_id) or string_id in ids or not isinstance(text, str):
            raise NativeCodecError("Invalid or duplicate string ID/text")
        ids.add(string_id)
    keys = set()
    for key in record.keys:
        key_hash, string_id = key
        # Real game containers also hold references to strings in other resources.
        if (not _uint(key_hash) or not _uint(string_id) or key in keys):
            raise NativeCodecError("Invalid or duplicate key association")
        keys.add(key)


class _Reader:
    def __init__(self, data):
        self.data = data
        self.position = 0
        self.limit = len(data) - 2

    def take(self, length):
        if length < 0 or self.position + length > self.limit:
            raise NativeCodecError("Truncated container or section overrun")
        result = self.data[self.position:self.position + length]
        self.position += length
        return result

    def count(self):
        value = shift = 0
        for index in range(1, 7):
            byte = self.take(1)[0]
            mask, step = ((0x7f, 7) if byte > 127 else
                          (0x3f, 6) if byte > 63 and index == 1 else (0xff, 6))
            value |= (byte & mask) << shift
            shift += step
            if byte < 64 or (index >= 3 and byte < 128):
                if value > 0xffffffff:
                    break
                return value
        raise NativeCodecError("Invalid bit6 section count")


def _count(value):
    if not _uint(value):
        raise NativeCodecError("Section count exceeds uint32")
    if value < 64:
        return bytes([value])
    # A short terminator (<64) plus as many seven-bit interior groups as needed.
    groups = [0x40 | (value & 0x3f)]
    value >>= 6
    while value >= 64:
        groups.append(0x80 | (value & 0x7f))
        value >>= 7
    groups.append(value)
    return bytes(groups)


def _crypt(data, magic, unit):
    result = bytearray(data)
    length = len(result) // unit
    key = (magic >> 8) & 0xffff
    for i in range(length):
        character_key = (length + 1) * key
        result[i * unit] ^= character_key & 0xff
        if unit == 2:
            result[i * unit + 1] ^= (character_key >> 8) & 0xff
        key = ((key << 1) | (key >> 15)) & 0xffff
    return bytes(result)


def decode(data: bytes) -> StringsFile:
    """Decode a bounded binary container without changing text code points."""
    try:
        if len(data) < 15 or data[:4] != b"RTSW":
            raise NativeCodecError("Invalid W3Strings header")
        reader = _Reader(data)
        reader.take(4)
        version, head = struct.unpack("<IH", reader.take(6))
        language_key = (head << 16) | struct.unpack("<H", data[-2:])[0]
        _validate(StringsFile(version, language_key, (), ()))
        magic = LANGUAGE_MAGICS[language_key]
        unit = 1 if version == 164 else 2
        count = reader.count()
        if count * 12 > reader.limit - reader.position:
            raise NativeCodecError("String section overrun")
        entries = [struct.unpack("<III", reader.take(12)) for _ in range(count)]
        count = reader.count()
        if count * 8 > reader.limit - reader.position:
            raise NativeCodecError("Key section overrun")
        keys = tuple((key_hash, string_id ^ magic) for key_hash, string_id in
                     (struct.unpack("<II", reader.take(8)) for _ in range(count)))
        units = reader.count()
        buffer = reader.take(units * unit)
        if reader.position != reader.limit:
            raise NativeCodecError("Unexpected bytes after string buffer")
        strings = []
        for string_id, offset, length in entries:
            if offset + length > units:
                raise NativeCodecError("String entry points outside buffer")
            raw = buffer[offset * unit:(offset + length) * unit]
            text = _crypt(raw, magic, unit).decode("utf-8" if unit == 1 else "utf-16-le", "strict")
            strings.append((string_id ^ magic, text))
        record = StringsFile(version, language_key, tuple(sorted(strings)), tuple(sorted(keys)))
        _validate(record)
        return record
    except (UnicodeError, struct.error, TypeError, OverflowError) as error:
        raise NativeCodecError(f"Invalid W3Strings container: {error}") from error


def encode(record: StringsFile) -> bytes:
    """Write both lookup blocks in game binary-search order."""
    try:
        _validate(record)
        unit = 1 if record.version == 164 else 2
        magic = LANGUAGE_MAGICS[record.language_key]
        entries = bytearray()
        buffer = bytearray()
        # The game searches the on-disk string index by its obfuscated ID.
        # Official v164 resources order this block by (ID ^ language magic).
        ordered_strings = sorted(record.strings, key=lambda item: item[0] ^ magic)
        for string_id, text in ordered_strings:
            raw = text.encode("utf-8" if unit == 1 else "utf-16-le", "strict")
            entries += struct.pack("<III", string_id ^ magic, len(buffer) // unit, len(raw) // unit)
            buffer += _crypt(raw, magic, unit) + bytes(unit)
        keys = b"".join(struct.pack("<II", key_hash, string_id ^ magic)
                        for key_hash, string_id in sorted(record.keys))
        return (b"RTSW" + struct.pack("<IH", record.version, record.language_key >> 16)
                + _count(len(record.strings)) + entries + _count(len(record.keys)) + keys
                + _count(len(buffer) // unit) + buffer
                + struct.pack("<H", record.language_key & 0xffff))
    except (UnicodeError, struct.error, TypeError, OverflowError) as error:
        raise NativeCodecError(f"Cannot encode W3Strings: {error}") from error


class NativeW3StringsCodec:
    """Generation adapter with an auditable implementation file identity."""
    executable = Path(__file__).resolve()
    version = CODEC_VERSION
    native = True

    def decode(self, source: Path, work_dir: Path) -> StringsFile:
        return decode(Path(source).read_bytes())

    def encode(self, record: StringsFile, work_dir: Path) -> Path:
        directory = Path(work_dir)
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "encoded.w3strings"
        output.write_bytes(encode(record))
        return output
