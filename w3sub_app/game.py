"""Read-only validation and inventory of Remastered 5.0 installations."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Callable, Sequence

from .models import GameInstallation, GameVersion, ResourceFingerprint, Storefront


VersionReader = Callable[[Path], str]


def _documents_directories() -> tuple[Path, ...]:
    """Return plausible Windows Documents folders, including redirected ones."""
    candidates: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                personal, _ = winreg.QueryValueEx(key, "Personal")
            candidates.append(Path(os.path.expandvars(personal)).expanduser())
        except (ImportError, OSError, TypeError, ValueError):
            pass
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root).expanduser() / "Documents")
    candidates.append(Path.home() / "Documents")

    unique = {}
    for candidate in candidates:
        try:
            unique.setdefault(os.path.normcase(str(candidate.resolve())), candidate)
        except OSError:
            continue
    return tuple(unique.values())


def _witcher_user_settings_path() -> Path | None:
    for documents in _documents_directories():
        path = documents / "The Witcher 3" / "user.settings"
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def detect_configured_text_language(settings_path: Path | None = None) -> str | None:
    """Read the active text language from the user's Witcher 3 settings file.

    The game install contains resources for many languages, so its available
    ``.w3strings`` files do not identify the user's selected language. Prefer
    the effective ``TextLanguage`` setting and use ``RequestedTextLanguage``
    only when the effective value is absent.
    """
    path = Path(settings_path) if settings_path is not None else _witcher_user_settings_path()
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    text = None
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None:
        return None

    localization: dict[str, str] = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            continue
        if section != "localization" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().casefold()
        if key in ("textlanguage", "requestedtextlanguage"):
            localization[key] = value.strip().strip('"').strip("'")

    for key in ("textlanguage", "requestedtextlanguage"):
        value = localization.get(key, "").strip().casefold()
        if re.fullmatch(r"[a-z]{2,4}", value):
            return value
    return None


def read_executable_version(path: Path) -> str:
    """Read FileVersion (or ProductVersion) without truncating string components.

    Windows' fixed numeric version words are only 16 bits each; the game's
    revision exceeds that range, so prefer the full version string resource.
    """
    if os.name != "nt":
        raise OSError("Executable version reading requires Windows")
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())

    def query(key):
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not api.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(length)):
            return None, 0
        return pointer, length.value

    pointer, length = query(r"\VarFileInfo\Translation")
    if pointer and length >= 4:
        translations = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
        for field in ("FileVersion", "ProductVersion"):
            for index in range(length // 4):
                language, codepage = translations[index * 2], translations[index * 2 + 1]
                value, count = query(f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}")
                if value and count:
                    text = ctypes.wstring_at(value, count).removesuffix("\0")
                    if text:
                        return text
    # Some executables have only the fixed resource. Retain all four words.
    pointer, length = query("\\")
    if pointer and length >= 52:
        words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
        if words[0] == 0xFEEF04BD:
            high, low = words[2], words[3]
            return f"{high >> 16}.{high & 0xffff}.{low >> 16}.{low & 0xffff}"
    raise OSError(f"No executable version information: {path}")


def find_game_executable(root: Path) -> Path:
    for directory in ("x64_dx12", "x64"):
        path = root / "bin" / directory / "witcher3.exe"
        if path.is_file():
            return path
    raise FileNotFoundError(f"No supported Witcher 3 executable in {root}")


def scan_game(root: Path, storefront: Storefront, store_build_id: str | None = None,
              version_reader: VersionReader | None = None) -> GameInstallation:
    root = root.resolve()
    if not (root / "content").is_dir():
        raise ValueError(f"Game folder requires a content directory: {root}")
    executable = find_game_executable(root)
    version = (version_reader or read_executable_version)(executable)
    raw_version = version
    # The shipped FileVersion appends '(Build Machine)'. Preserve every
    # numeric version component while excluding this non-version annotation.
    match = re.fullmatch(r"((\d+)\.(\d+)\.\d+\.\d+)(?:\s*\([^()]*\))?", version.strip())
    if not match or (int(match[2]), int(match[3])) != (5, 0):
        raise ValueError(f"Unsupported executable version {version!r}; Remastered 5.0 is required")
    version = match[1]
    languages: dict[str, list[Path]] = {}

    def fail(error):
        raise error

    for directory in (root / "content", root / "dlc"):
        try:
            metadata = directory.stat()
        except FileNotFoundError:
            if directory == root / "dlc":
                continue
            raise
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Resource root is not a directory: {directory}")
        directory.resolve().relative_to(root)
        for parent, _, names in os.walk(directory, onerror=fail, followlinks=False):
            for name in names:
                path = Path(parent) / name
                if path.suffix.casefold() != ".w3strings" or not re.fullmatch(r"[a-z]{2,4}", path.stem, re.I):
                    continue
                path.resolve().relative_to(root)
                languages.setdefault(path.stem.casefold(), []).append(path)
    inventory = {language: tuple(sorted(paths, key=lambda p: p.relative_to(root).as_posix()))
                 for language, paths in sorted(languages.items())}
    return GameInstallation(root, storefront, GameVersion(version, store_build_id, raw_version), inventory)


def fingerprint_files(root: Path, paths: Sequence[Path]) -> ResourceFingerprint:
    """Hash a complete logical inventory; read errors propagate to the caller.

    Aggregate framing is path byte length, UTF-8 POSIX path, then raw SHA-256.
    This binds both the inventory and every file's bytes without ambiguity.
    Paths may be absolute or relative to root; duplicates denote one resource.
    """
    root = root.resolve()
    inventory = {}
    for path in paths:
        path = Path(path)
        absolute = (path if path.is_absolute() else root / path).resolve()
        inventory[absolute.relative_to(root).as_posix()] = absolute
    entries = {}
    aggregate = hashlib.sha256()
    for relative, absolute in sorted(inventory.items()):
        digest = hashlib.sha256()
        with absolute.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        entries[relative] = digest.hexdigest()
        encoded = relative.encode("utf-8")
        aggregate.update(len(encoded).to_bytes(8, "big"))
        aggregate.update(encoded)
        aggregate.update(digest.digest())
    return ResourceFingerprint(entries, aggregate.hexdigest())
