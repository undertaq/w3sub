"""Read local storefront records without launching clients or scanning drives.

Returned roots pass layout checks only. The game module performs executable
version validation before the application can generate or install anything.
"""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from .models import GameCandidate, Storefront


class RegistryReader(Protocol):
    def values(self, hive: str, view: int, key: str) -> Mapping[str, object]: ...
    def subkeys(self, hive: str, view: int, key: str) -> list[str]: ...


@dataclass(frozen=True)
class StoreRoots:
    """Explicit local record roots; injected roots suppress environment defaults."""
    steam_roots: tuple[Path, ...] = ()
    epic_manifest_roots: tuple[Path, ...] = ()


class WindowsRegistry:
    def __init__(self):
        import winreg
        self.reg = winreg

    def _open(self, hive, view, key):
        reg = self.reg
        access = reg.KEY_READ | (reg.KEY_WOW64_32KEY if view == 32 else reg.KEY_WOW64_64KEY)
        return reg.OpenKey(getattr(reg, "HKEY_CURRENT_USER" if hive == "HKCU" else "HKEY_LOCAL_MACHINE"), key, 0, access)

    def values(self, hive, view, key):
        values = {}
        try:
            with self._open(hive, view, key) as handle:
                for index in range(self.reg.QueryInfoKey(handle)[1]):
                    name, value, _ = self.reg.EnumValue(handle, index)
                    values[name] = value
        except OSError:
            pass
        return values

    def subkeys(self, hive, view, key):
        try:
            with self._open(hive, view, key) as handle:
                return [self.reg.EnumKey(handle, i) for i in range(self.reg.QueryInfoKey(handle)[0])]
        except OSError:
            return []


def _vdf(text: str) -> dict:
    # VDF strings escape backslashes and quotes. Comments are ignored only
    # outside quoted strings, preserving path and URL content.
    tokens = re.finditer(r'"((?:\\.|[^"\\])*)"|([{}])|//[^\n]*', text)
    items = [match.group(2) if match.group(2) else
             re.sub(r'\\([\\"])', r'\1', match.group(1))
             for match in tokens if match.group(1) is not None or match.group(2)]
    position = 0

    def read(nested=False):
        nonlocal position
        result = {}
        while position < len(items):
            key = items[position]
            position += 1
            if key == "}":
                if nested:
                    return result
                raise ValueError("unexpected VDF close")
            if key == "{" or position == len(items):
                raise ValueError("invalid VDF pair")
            value = items[position]
            position += 1
            if value == "{":
                value = read(True)
            elif value == "}":
                raise ValueError("missing VDF value")
            result[key.casefold()] = value
        if nested:
            raise ValueError("unclosed VDF record")
        return result

    return read()


def _read_vdf(path):
    try:
        return _vdf(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}


def _path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(os.path.expandvars(value.strip().strip('"'))).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _witcher(value):
    return isinstance(value, str) and bool(re.search(r"\b(?:the\s+)?witcher\s*3\b", value, re.I))


def _values(registry, hive, view, key):
    return {name.casefold(): value for name, value in registry.values(hive, view, key).items()}


def _build(values):
    for name in ("buildid", "appversion", "version", "displayversion", "build"):
        value = values.get(name)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def discover_candidates(registry: RegistryReader | None = None,
                        roots: StoreRoots | None = None) -> list[GameCandidate]:
    if registry is None:
        if os.name != "nt":
            return []
        registry = WindowsRegistry()
    if roots is None:
        program_data = os.environ.get("ProgramData")
        roots = StoreRoots(epic_manifest_roots=(
            (Path(program_data) / "Epic/EpicGamesLauncher/Data/Manifests",)
            if program_data else ()))
    candidates = {}

    def add(path, store, source, build=None):
        root = _path(str(path)) if path is not None else None
        try:
            if root is None or not (root / "content").is_dir():
                return
            if not any((root / "bin" / directory / "witcher3.exe").is_file()
                       for directory in ("x64", "x64_dx12")):
                return
        except OSError:
            return
        canonical = os.path.normcase(str(root))
        existing = candidates.get(canonical)
        if existing:
            sources = existing.record_source.split("; ")
            if source not in sources:
                sources.append(source)
            candidates[canonical] = replace(existing, record_source="; ".join(sources),
                                             store_build_id=existing.store_build_id or build)
        else:
            candidates[canonical] = GameCandidate(root, store, source, build)

    steam_roots = set(roots.steam_roots)
    epic_roots = set(roots.epic_manifest_roots)
    for hive in ("HKCU", "HKLM"):
        for view in (32, 64):
            def records(key):
                yield key, _values(registry, hive, view, key)
                for child in registry.subkeys(hive, view, key):
                    child_key = key + "\\" + child
                    yield child_key, _values(registry, hive, view, child_key)

            for key, values in records(r"Software\Valve\Steam"):
                for field in ("steampath", "installpath"):
                    path = _path(values.get(field))
                    if path:
                        steam_roots.add(path)
            for base in (r"Software\GOG.com\Games",
                         r"Software\Microsoft\Windows\CurrentVersion\Uninstall"):
                for key, values in records(base):
                    if base.endswith("Uninstall") and not (
                            any("gog.com" in value.casefold() for value in values.values()
                                if isinstance(value, str))
                            or "gog" in key.rsplit("\\", 1)[-1].casefold()
                            or key.rsplit("\\", 1)[-1].split("_")[0] in ("1207664643", "1495134320")):
                        continue
                    if not (_witcher(values.get("gamename")) or _witcher(values.get("displayname"))
                            or (base.endswith("Games") and key.rsplit("\\", 1)[-1] in ("1207664643", "1495134320"))):
                        continue
                    for field in ("path", "installlocation", "installpath"):
                        add(_path(values.get(field)), Storefront.GOG,
                            f"{hive} ({view}-bit) {key} [{field}]", _build(values))
            for key, values in records(r"Software\Epic Games"):
                if _witcher(key.rsplit("\\", 1)[-1]) or _witcher(values.get("displayname")):
                    add(_path(values.get("installlocation")), Storefront.EPIC,
                        f"{hive} ({view}-bit) {key}", _build(values))
                if key.rsplit("\\", 1)[-1].casefold() == "epicgameslauncher":
                    for field in ("appdatapath", "installlocation"):
                        path = _path(values.get(field))
                        if path:
                            epic_roots.update((path / "Data/Manifests", path / "Manifests"))

    libraries = set(steam_roots)
    for client in sorted(steam_roots, key=str):
        record = _read_vdf(client / "steamapps/libraryfolders.vdf").get("libraryfolders", {})
        if isinstance(record, dict):
            for number, library in record.items():
                if number.isdigit():
                    path = _path(library.get("path") if isinstance(library, dict) else library)
                    if path:
                        libraries.add(path)
    for library in sorted(libraries, key=str):
        manifest = library / "steamapps/appmanifest_292030.acf"
        record = _read_vdf(manifest).get("appstate", {})
        if not isinstance(record, dict) or record.get("appid") != "292030":
            continue
        directory = record.get("installdir")
        if not isinstance(directory, str) or not directory or directory in (".", "..") or any(c in directory for c in "/\\:"):
            continue
        add(library / "steamapps/common" / directory, Storefront.STEAM, str(manifest), _build(record))
    for directory in sorted(epic_roots, key=str):
        try:
            manifests = sorted(directory.glob("*.item"))
        except OSError:
            continue
        for manifest in manifests:
            try:
                record = json.loads(manifest.read_text(encoding="utf-8-sig"))
                if not isinstance(record, dict):
                    continue
                values = {name.casefold(): value for name, value in record.items()}
                if _witcher(values.get("displayname")):
                    add(_path(values.get("installlocation")), Storefront.EPIC, str(manifest), _build(values))
            except (OSError, UnicodeError, ValueError):
                continue
    return sorted(candidates.values(), key=lambda candidate: os.path.normcase(str(candidate.root)))
