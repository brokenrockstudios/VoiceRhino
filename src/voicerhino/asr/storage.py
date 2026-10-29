"""What's in the models folder: names, sizes, and which entries the settings use.

Settings › General lists these so unused downloads can be removed; a removed model
downloads again if it's picked later. Nothing outside the models folder is ever deleted.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..paths import models_dir
from ..translate.catalog import LLAMA_CPP, TRANSLATION_MODELS, download_names, get_translation_model
from .catalog import (
    ASR_MODELS,
    DEFAULT_MODEL_ID,
    DEFAULT_SPEAKER_MODEL,
    SPEAKER_MODELS,
    STREAMING_MODELS,
    VAD_MODELS,
    get_model,
    supports_live_model,
)
from .download import local_name

if TYPE_CHECKING:
    from ..config import AppConfig

WHISPER_DIR = "faster-whisper"  # faster-whisper's own cache, one models--org--name folder per model
VAD_KEY = "vad"
TRANSLATE_PREFIX = "translate:"  # + the download's name in the models folder
_SPEAKER_NAMES = {"titanet-small": "Speaker model TitaNet-small (voice colors)",
                  "wespeaker-resnet34": "Speaker model WeSpeaker ResNet34 (an alternative to TitaNet)"}


@dataclass(frozen=True)
class Download:
    path: Path  # a file or folder in the models folder
    name: str  # what it is, for people
    size: int  # bytes on disk
    key: str = ""  # catalog model id, VAD_KEY, "speaker:<name>" or "translate:<name>"; "" = unknown
    unfinished: bool = False  # an interrupted download or extraction


def _known() -> dict[str, tuple[str, str]]:
    """Path relative to the models folder -> (key, display name)."""
    known = {}
    for spec in ASR_MODELS + STREAMING_MODELS:
        if spec.family == "faster_whisper":
            known[f"{WHISPER_DIR}/models--{spec.source.replace('/', '--')}"] = (spec.id, spec.name)
        else:
            known[local_name(spec.source)] = (spec.id, spec.name)
    for url in VAD_MODELS.values():
        known[local_name(url)] = (VAD_KEY, "Silero voice detector (finds speech; always needed)")
    for name, url in SPEAKER_MODELS.items():
        known[local_name(url)] = (f"speaker:{name}", _SPEAKER_NAMES.get(name, f"Speaker model {name}"))
    # Portuguese and "Other languages" share one download: it's listed under the latter.
    for spec in reversed(TRANSLATION_MODELS):
        for path in download_names(spec):
            if path not in known:
                known[path] = (TRANSLATE_PREFIX + path, f"Translation: {spec.name}")
    known[local_name(LLAMA_CPP)] = (TRANSLATE_PREFIX + local_name(LLAMA_CPP),
                                    "llama.cpp (runs the Hy-MT2 translation model)")
    return known


def _size(path: Path) -> int:
    if not path.is_dir():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def list_downloads(root: Path | None = None) -> list[Download]:
    """Everything in the models folder, largest first."""
    root = root or models_dir()
    known = _known()
    entries: list[tuple[str, Path]] = []
    for path in root.iterdir():
        if path.name == WHISPER_DIR and path.is_dir():
            entries += [(f"{WHISPER_DIR}/{p.name}", p) for p in path.glob("models--*")]
        else:
            entries.append((path.name, path))
    items = []
    for rel, path in entries:
        try:
            size = _size(path)
        except OSError:
            continue  # vanished while we looked
        whisper = rel.startswith(f"{WHISPER_DIR}/")
        stem = rel.removesuffix(".part").removesuffix(".extracting")
        model = stem if whisper else local_name(stem)  # an archive is only used once extracted
        key, name = known.get(model, ("", model.removeprefix(f"{WHISPER_DIR}/models--").replace("--", "/")))
        unfinished = (model != rel  # .part file, .extracting folder or a leftover archive
                      or (path.is_dir() and key and not whisper and not (path / ".complete").exists()))
        if unfinished:
            items.append(Download(path, f"Unfinished download: {name}", size, unfinished=True))
        else:
            items.append(Download(path, name, size, key))
    return sorted(items, key=lambda d: -d.size)


def used_keys(config: AppConfig) -> set[str]:
    """The downloads these settings need."""
    try:
        spec = get_model(config.asr.model)
    except KeyError:
        spec = get_model(DEFAULT_MODEL_ID)  # what the app falls back to
    keys = {VAD_KEY, spec.id}
    if config.asr.live_model and supports_live_model(spec):
        keys.add(config.asr.live_model)
    if config.speakers.voice_enabled:
        keys.add(f"speaker:{DEFAULT_SPEAKER_MODEL}")
    if config.translation.model:
        try:
            keys |= {TRANSLATE_PREFIX + p for p in download_names(get_translation_model(config.translation.model))}
        except KeyError:
            pass
    return keys


def delete_download(path: Path, root: Path | None = None) -> None:
    """Remove a downloaded file or folder. Refuses anything that isn't inside the models
    folder (symlinks are resolved first), and the faster-whisper cache folder itself."""
    root = (root or models_dir()).resolve()
    target = path.resolve()
    if root not in target.parents or target == root / WHISPER_DIR:
        raise ValueError(f"not a download in {root}: {path}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def format_size(size: int) -> str:
    """Like Explorer: 1024-based, "1.6 GB", "631 MB", "632 KB"."""
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.0f} MB"
    return f"{-(-size // 1024)} KB"
