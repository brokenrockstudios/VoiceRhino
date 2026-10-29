"""Download model files/archives into the models folder, resumably and atomically."""

from __future__ import annotations

import shutil
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .. import __version__
from ..paths import models_dir

# progress(done_bytes, total_bytes); total is 0 when the server doesn't say.
Progress = Callable[[int, int], None]

_ARCHIVE_SUFFIXES = (".tar.bz2", ".tar.gz", ".tgz", ".tar", ".zip")
_COMPLETE_MARKER = ".complete"
_CHUNK = 1 << 20


class DownloadCancelled(Exception):
    pass


def _strip_archive_suffix(filename: str) -> str:
    for suffix in _ARCHIVE_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def local_name(url: str) -> str:
    """The file or folder name `url` ends up as in the models folder (archives are extracted)."""
    return _strip_archive_suffix(url.rsplit("/", 1)[-1])


def download_file(
    url: str,
    dest: Path,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Fetch url to dest via a .part file, resuming a previous partial download."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": f"VoiceRhino/{__version__}"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        if have and response.status != 206:  # server ignored Range: start over
            have = 0
        total = int(response.headers.get("Content-Length") or 0) + have
        with open(part, "ab" if have else "wb") as out:
            done = have
            while chunk := response.read(_CHUNK):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled(url)
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    part.replace(dest)
    return dest


def ensure_download(
    url: str,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    root: Path | None = None,
) -> Path:
    """Return the local path for url, downloading (and extracting archives) if needed.

    A single file lands at <root>/<filename>. An archive is extracted to
    <root>/<archive name without suffix>/ and that folder is returned.
    """
    root = root or models_dir()
    filename = url.rsplit("/", 1)[-1]
    target_name = _strip_archive_suffix(filename)
    if target_name == filename:
        target = root / filename
        if not target.exists():
            download_file(url, target, progress, cancel)
        return target

    folder = root / target_name
    if (folder / _COMPLETE_MARKER).exists():
        return folder
    archive = download_file(url, root / filename, progress, cancel)
    staging = root / (target_name + ".extracting")
    shutil.rmtree(staging, ignore_errors=True)
    if filename.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)  # drops absolute paths and ".." parts
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(staging, filter="data")
    # Archives usually contain a single top-level folder; flatten it if so.
    entries = list(staging.iterdir())
    source = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
    shutil.rmtree(folder, ignore_errors=True)
    shutil.move(str(source), str(folder))
    shutil.rmtree(staging, ignore_errors=True)
    (folder / _COMPLETE_MARKER).touch()
    archive.unlink(missing_ok=True)
    return folder


def ensure_folder(
    name: str,
    urls: tuple[str, ...] | list[str],
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    root: Path | None = None,
) -> Path:
    """Download several files (e.g. one Hugging Face model repo) into <root>/<name>/."""
    folder = (root or models_dir()) / name
    if (folder / _COMPLETE_MARKER).exists():
        return folder
    for url in urls:
        target = folder / url.rsplit("/", 1)[-1]
        if not target.exists():
            download_file(url, target, progress, cancel)
    (folder / _COMPLETE_MARKER).touch()
    return folder


def is_folder_downloaded(name: str, root: Path | None = None) -> bool:
    return ((root or models_dir()) / name / _COMPLETE_MARKER).exists()


def is_downloaded(url: str, root: Path | None = None) -> bool:
    root = root or models_dir()
    filename = url.rsplit("/", 1)[-1]
    target_name = _strip_archive_suffix(filename)
    if target_name == filename:
        return (root / filename).exists()
    return (root / target_name / _COMPLETE_MARKER).exists()
