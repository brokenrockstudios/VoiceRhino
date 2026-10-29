"""User settings, persisted as JSON in %APPDATA%\\VoiceRhino\\config.json.

Loading tolerates missing and unknown keys, so old config files keep working
as fields are added.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

from .asr.catalog import DEFAULT_LIVE_MODEL_ID, DEFAULT_MODEL_ID
from .audio.devices import DEFAULT_OUTPUT_KEY
from .paths import config_dir
from .pipeline import PipelineSettings
from .translate import TranslationConfig

log = logging.getLogger(__name__)

CONFIG_VERSION = 3  # bump when saved values need migrating (see _migrate)


@dataclass
class SourceConfig:
    kind: str = "device"  # "device" (input or output loopback), "app" (per-app capture), "file"
    device: str = DEFAULT_OUTPUT_KEY  # kind=device: key from audio.devices
    app: str = "Discord.exe"  # kind=app: executable name, children included
    file: str = ""  # kind=file: WAV path (demo/testing), loops
    name: str = ""  # short label shown next to captions when several sources run
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])  # stable, for ViewConfig.hidden_sources

    def describe(self) -> str:
        if self.name:
            return self.name
        if self.kind == "app":
            return self.app.removesuffix(".exe")
        if self.kind == "file":
            return Path(self.file).name or "file"
        if self.device == DEFAULT_OUTPUT_KEY:
            return "Default output"
        return self.device.split(":", 1)[-1]


@dataclass
class AsrConfig:
    model: str = DEFAULT_MODEL_ID
    device: str = "cpu"  # "cpu" or "cuda"
    threads: int = 2
    language: str = "en"  # "" = auto-detect (multilingual models)
    task: str = "transcribe"  # Whisper: "translate" = any language -> English
    # "" (default) = re-decode the live line with the main model. A streaming model id = two-pass:
    # it writes the live line word by word and the main model rewrites each finished line.
    live_model: str = DEFAULT_LIVE_MODEL_ID


@dataclass
class ViewConfig:
    """One on-screen window: a big caption overlay or a scrollback history."""

    kind: str = "caption"  # "caption" or "history"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    visible: bool = True
    x: int = -1  # x == y == -1: place automatically on first show (other negatives are real
    y: int = -1  # positions on monitors left of / above the primary one)
    w: int = 1100
    h: int = 160
    font_family: str = "Segoe UI"
    font_size: int = 34
    bold: bool = True
    text_color: str = "#FFFFFF"
    partial_color: str = "#D0D0D0"  # the still-changing live words
    outline: int = 3  # text outline width in px, 0 = none
    background: str = "#000000"
    background_opacity: float = 0.45
    click_through: bool = True  # when overlays are locked
    lines: int = 2  # caption: lines visible at once (roll-up)
    clear_after: float = 8.0  # caption: hide text after this many quiet seconds, 0 = never
    history_seconds: int = 60  # history: how far back to show
    show_timestamps: bool = True  # history
    show_speakers: bool = True
    show_warnings: bool = True  # e.g. "Discord is very quiet"; caption windows show it only with captions
    # SourceConfig ids this window leaves out (e.g. your own mic in the big captions). A list of
    # hidden rather than shown sources, so a source added later shows everywhere until unticked.
    hidden_sources: list[str] = field(default_factory=list)

    @staticmethod
    def default_history() -> "ViewConfig":
        return ViewConfig(kind="history", w=520, h=420, font_size=15, bold=False,
                          outline=0, background_opacity=0.7, click_through=False)


@dataclass
class SpeakerConfig:
    voice_enabled: bool = False  # color speakers by voice similarity
    voice_threshold: float = 0.0  # 0 = the model's tuned default
    discord_enabled: bool = False  # names from Discord's local RPC
    discord_client_id: str = ""
    discord_client_secret: str = ""  # stays on this PC; see docs/discord-setup.md


@dataclass
class AppConfig:
    sources: list[SourceConfig] = field(default_factory=lambda: [SourceConfig()])
    asr: AsrConfig = field(default_factory=AsrConfig)
    pipeline: PipelineSettings = field(default_factory=PipelineSettings)
    views: list[ViewConfig] = field(default_factory=lambda: [ViewConfig(), ViewConfig.default_history()])
    speakers: SpeakerConfig = field(default_factory=SpeakerConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    locked: bool = False  # overlays click-through and borderless
    lock_hotkey: str = "Ctrl+Alt+C"
    # (A "save_transcript" option existed until session 2; old files' value is ignored.)
    captions_enabled: bool = True
    version: int = CONFIG_VERSION


def _migrate(data: dict) -> dict:
    """Bring settings saved by an older version up to date."""
    if data.get("version", 1) < 2:
        pipeline = data.get("pipeline")
        # 0.25 s was the old default (not editable then); it dropped short words like "Wait,".
        if isinstance(pipeline, dict) and pipeline.get("min_speech") == 0.25:
            pipeline["min_speech"] = PipelineSettings.min_speech
    if data.get("version", 1) < 3 and isinstance(data.get("translation"), dict):
        # Translation used to go through an LLM server or DeepL; it's on-PC models now. The old
        # "model" was a server's model name, so translation starts off; the API key is dropped.
        data["translation"] = {"only_foreign": data["translation"].get("only_foreign", True)}
    data["version"] = CONFIG_VERSION
    return data


def _from_dict(cls, data: Any):
    """Build dataclass `cls` from parsed JSON, ignoring unknown keys."""
    if not isinstance(data, dict):
        return cls()
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value, hint = data[f.name], hints[f.name]
        origin = getattr(hint, "__origin__", None)
        if is_dataclass(hint):
            value = _from_dict(hint, value)
        elif origin is list and is_dataclass(hint.__args__[0]):
            value = [_from_dict(hint.__args__[0], v) for v in value or []]
        kwargs[f.name] = value
    return cls(**kwargs)


def config_path() -> Path:
    return config_dir() / "config.json"


def load(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _from_dict(AppConfig, _migrate(data) if isinstance(data, dict) else data)
    except FileNotFoundError:
        return AppConfig()
    except (OSError, ValueError, TypeError):
        log.exception("couldn't read %s; using defaults", path)
        return AppConfig()


def save(config: AppConfig, path: Path | None = None) -> None:
    path = path or config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    tmp.replace(path)
