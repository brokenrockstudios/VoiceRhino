"""Where VoiceRhino keeps settings, models and logs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "VoiceRhino"


def is_frozen() -> bool:
    """True in the portable build (VoiceRhino.exe from PyInstaller), False when run from a venv."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The folder VoiceRhino runs from: the one holding VoiceRhino.exe, or the source checkout."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _env_dir(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    path = Path(value) if value else fallback
    path.mkdir(parents=True, exist_ok=True)
    return path


def running_from_temp() -> bool:
    """The portable exe started from a temp folder: double-clicked inside the zip, so Windows
    unpacked only that one file (or the whole zip, to be deleted again) somewhere temporary."""
    if not is_frozen():
        return False
    import tempfile

    try:
        return Path(sys.executable).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except OSError:
        return False


def config_dir() -> Path:
    """Settings (config.json, Discord token cache)."""
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return _env_dir("VOICERHINO_CONFIG", base / APP_NAME)


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return _env_dir("VOICERHINO_DATA", base / APP_NAME)


def models_dir() -> Path:
    """Downloaded models; override with VOICERHINO_MODELS to share a folder."""
    return _env_dir("VOICERHINO_MODELS", data_dir() / "models")


def logs_dir() -> Path:
    return _env_dir("VOICERHINO_LOGS", data_dir() / "logs")
