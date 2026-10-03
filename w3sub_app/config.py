"""Small, credential-free settings and app-data path helpers."""
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import tempfile

from .models import AppConfig


APP_DIRECTORY = "W3DualSubtitle"
CONFIG_FILENAME = "config.json"
_LOGGER_NAME = "w3dual_subtitle"


def app_state_root() -> Path:
    """Return the per-user state root, using LOCALAPPDATA on Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data and local_app_data.strip():
        return Path(local_app_data).expanduser() / APP_DIRECTORY
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile) / "AppData" / "Local" / APP_DIRECTORY
    return Path.home() / ".local" / "share" / APP_DIRECTORY


def _normalized_path_identity(path: Path) -> str:
    normalized = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(normalized)))


def state_root_for(game_root: Path) -> Path:
    """Return an isolated app-data folder keyed by normalized game path."""
    identity = _normalized_path_identity(Path(game_root))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return app_state_root() / "games" / key


def _is_game_folder(path: Path) -> bool:
    try:
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir() or not (root / "content").is_dir():
            return False
        return any((root / "bin" / architecture / "witcher3.exe").is_file()
                   for architecture in ("x64_dx12", "x64"))
    except (OSError, RuntimeError, TypeError):
        return False


def _existing_file_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
        return path if path.is_file() else None
    except (OSError, RuntimeError, TypeError):
        return None


def _config_path() -> Path:
    return app_state_root() / CONFIG_FILENAME


def load_config() -> AppConfig:
    """Load valid selections; malformed or stale settings fall back safely."""
    try:
        payload = json.loads(_config_path().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return AppConfig()
        raw_root = payload.get("last_game_root")
        root = None
        if isinstance(raw_root, str) and raw_root.strip() and _is_game_folder(Path(raw_root)):
            root = Path(raw_root).expanduser().resolve()
        converter = _existing_file_path(payload.get("converter_path"))
        return AppConfig(root, converter)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return AppConfig()


def _validated_config(config: AppConfig) -> AppConfig:
    root = config.last_game_root
    converter = config.converter_path
    if root is not None:
        root = Path(root).expanduser().resolve() if _is_game_folder(Path(root)) else None
    if converter is not None:
        converter = _existing_file_path(str(converter))
    return AppConfig(root, converter)


def save_config(config: AppConfig) -> None:
    """Atomically save only the selected game folder and converter path."""
    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    root = app_state_root().expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    normalized = _validated_config(config)
    payload = {
        "converter_path": str(normalized.converter_path) if normalized.converter_path else None,
        "last_game_root": str(normalized.last_game_root) if normalized.last_game_root else None,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix=".config-",
                suffix=".tmp", dir=root, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / CONFIG_FILENAME)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def configure_logging() -> logging.Logger:
    """Write rotating diagnostics under app data, without logging game data."""
    root = app_state_root().expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    expected = (root / "w3dual-subtitle.log").resolve()
    if not any(getattr(handler, "baseFilename", None) == str(expected)
               for handler in logger.handlers):
        handler = RotatingFileHandler(expected, maxBytes=2_000_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(threadName)s %(message)s"
        ))
        logger.addHandler(handler)
    return logger
