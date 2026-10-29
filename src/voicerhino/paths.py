"""Where VoiceRhino keeps settings, models and logs."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "VoiceRhino"


def _env_dir(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    path = Path(value) if value else fallback
    path.mkdir(parents=True, exist_ok=True)
    return path


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
