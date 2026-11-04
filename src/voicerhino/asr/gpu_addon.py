"""The GPU add-on of the portable build: faster-whisper + NVIDIA's CUDA libraries, on demand.

VoiceRhino.exe ships without them (they're ~1.3 GB). packaging/build.py writes gpu_addon.json from
uv.lock: the exact wheels (URL, sha256, size) the `gpu` extra adds to what the exe already has.
Picking a GPU model downloads those wheels from PyPI into the models folder, checks each hash and
unpacks them (a wheel is a zip), then adds the folder to sys.path. Nothing here runs from a venv:
there `uv sync --extra gpu` installs the same packages.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import threading
import zipfile
from pathlib import Path

from ..paths import is_frozen, models_dir
from .download import DownloadCancelled, Progress, download_file

log = logging.getLogger(__name__)

FOLDER = "gpu-addon"  # in the models folder
KEY = "gpu-addon"  # storage key (Settings › General downloads list)
MANIFEST = "gpu_addon.json"
_COMPLETE = ".complete"  # holds the manifest key of the installed set
_NVIDIA_BIN = ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc")
_activated = False


def manifest_path() -> Path | None:
    """The manifest bundled with VoiceRhino.exe (None when running from a venv)."""
    if not is_frozen():
        return None
    path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / MANIFEST
    return path if path.exists() else None


def load_manifest(path: Path | None = None) -> dict | None:
    path = path or manifest_path()
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def addon_dir(root: Path | None = None) -> Path:
    return (root or models_dir()) / FOLDER


def needed() -> bool:
    """True in the portable build, where GPU models need this add-on (venvs use the extra)."""
    return manifest_path() is not None


def total_mb(manifest: dict | None = None) -> int:
    manifest = manifest or load_manifest()
    return round(sum(w["size"] for w in manifest["wheels"]) / 1e6) if manifest else 0


def nvidia_driver_present() -> bool:
    """NVIDIA's driver installs nvcuda.dll; without it CUDA can't run at all."""
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return (system / "nvcuda.dll").exists()


def is_installed(manifest: dict | None = None, root: Path | None = None) -> bool:
    manifest = manifest or load_manifest()
    if manifest is None:
        return False
    marker = addon_dir(root) / _COMPLETE
    try:
        return marker.read_text(encoding="utf-8").strip() == manifest["key"]
    except OSError:
        return False


def available() -> bool:
    """Can faster-whisper be imported (installed add-on, or the venv's gpu extra)?"""
    if needed():
        return is_installed()
    return importlib.util.find_spec("faster_whisper") is not None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def install(progress: Progress | None = None, cancel: threading.Event | None = None,
            manifest: dict | None = None, root: Path | None = None) -> Path:
    """Download, verify and unpack every wheel; resumable (finished wheels are kept until the
    whole set is unpacked). Returns the add-on folder."""
    manifest = manifest or load_manifest()
    if manifest is None:
        raise RuntimeError("this build has no GPU add-on list")
    folder = addon_dir(root)
    if is_installed(manifest, root):
        return folder
    wheels_dir = folder / "wheels"
    staging = folder / "site.extracting"
    total = sum(w["size"] for w in manifest["wheels"])
    done = 0
    for wheel in manifest["wheels"]:
        target = wheels_dir / wheel["url"].rsplit("/", 1)[-1]
        if not (target.exists() and target.stat().st_size == wheel["size"]):
            offset = done
            download_file(wheel["url"], target,
                          (lambda d, _t, offset=offset: progress(offset + d, total)) if progress else None,
                          cancel)
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled(wheel["url"])
        if _sha256(target) != wheel["sha256"]:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"{target.name} didn't match its checksum; try again")
        done += wheel["size"]
        if progress:
            progress(done, total)
    if staging.exists():
        shutil.rmtree(staging)
    for wheel in manifest["wheels"]:
        with zipfile.ZipFile(wheels_dir / wheel["url"].rsplit("/", 1)[-1]) as zf:
            zf.extractall(staging)
    site = folder / "site"
    if site.exists():
        shutil.rmtree(site)  # an older set (a VoiceRhino update changed the versions)
    staging.rename(site)
    shutil.rmtree(wheels_dir, ignore_errors=True)
    (folder / _COMPLETE).write_text(manifest["key"], encoding="utf-8")
    log.info("GPU add-on installed (%d packages)", len(manifest["wheels"]))
    return folder


def activate(root: Path | None = None) -> bool:
    """Make an installed add-on importable and let CTranslate2 find the CUDA DLLs. Bundled
    packages (numpy, ctranslate2) come first on sys.path, so the add-on never replaces them."""
    global _activated
    if _activated:
        return True
    if not is_installed(root=root):
        return False
    site = addon_dir(root) / "site"
    sys.path.append(str(site))
    for sub in _NVIDIA_BIN:
        bin_dir = site / "nvidia" / sub / "bin"
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    importlib.invalidate_caches()
    _activated = True
    return True


def ensure(progress: Progress | None = None, cancel: threading.Event | None = None) -> None:
    """Before loading a GPU model: download the add-on if this build needs it, then activate it."""
    if not needed():
        return
    if not is_installed():
        if not nvidia_driver_present():
            raise RuntimeError("Whisper models need an NVIDIA graphics card (no NVIDIA driver found). "
                               "Pick a CPU model in Settings › Speech recognition.")
        install(progress, cancel)
    activate()
