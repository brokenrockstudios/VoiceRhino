"""One-click update of the portable build (Settings › General › Update), only when asked.

1. download(): fetch the release zip into <app folder>\\_update, check its SHA-256, unpack it.
2. launch_finish(): start the NEW VoiceRhino.exe from there with --finish-update; the app quits.
3. finish(), in that new exe: wait for the old app to exit, move the old files to _previous, copy
   the new ones in and run their --self-test. If anything fails, the old files go back and the old
   version starts again; otherwise _previous is deleted and the new version starts.

Only the files a release ships are replaced, so anything else in the folder stays. Settings,
models and logs live in %APPDATA% / %LOCALAPPDATA% and are never touched.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .asr.download import Progress, download_file
from .paths import app_dir, is_frozen
from .updates import Release

log = logging.getLogger(__name__)

STAGING = "_update"
PREVIOUS = "_previous"
EXE = "VoiceRhino.exe"
# What a release ships at the top of its folder; also replaced if a newer release drops one.
APP_ITEMS = {EXE, "_internal", "docs", "README.md", "LICENSE", "CHANGELOG.md", "START-HERE.txt"}


class UpdateError(Exception):
    pass


def supported() -> bool:
    """One-click updates need the portable build (a venv updates with git / uv)."""
    return is_frozen()


def check_writable(app: Path) -> None:
    probe = app / ".update-check"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise UpdateError(f"VoiceRhino can't write to its folder ({app}). Move the VoiceRhino folder "
                          "somewhere like C:\\VoiceRhino, or download the new zip from the release page.") from exc


def _expected_sha256(release: Release) -> str:
    if release.sha256:
        return release.sha256.lower()
    if release.sha256_url:
        request = urllib.request.Request(release.sha256_url, headers={"User-Agent": f"VoiceRhino/{__version__}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("ascii", "replace").split()[0].lower()
    raise UpdateError("this release doesn't publish a checksum; download it from the release page")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def download(release: Release, progress: Progress | None = None, cancel: threading.Event | None = None,
             app: Path | None = None) -> Path:
    """Download and unpack the release next to the app; returns the new version's folder."""
    app = app or app_dir()
    check_writable(app)
    if not release.zip_url:
        raise UpdateError("this release has no Windows zip; download it from the release page")
    staging = app / STAGING
    target = staging / release.zip_url.rsplit("/", 1)[-1]
    expected = _expected_sha256(release)
    download_file(release.zip_url, target, progress, cancel)
    if _sha256(target) != expected:
        target.unlink(missing_ok=True)
        raise UpdateError("the download didn't match its checksum; try again")
    unpacked = staging / "new"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    with zipfile.ZipFile(target) as zf:
        zf.extractall(unpacked)
    target.unlink()
    new = unpacked / "VoiceRhino"
    if not (new / EXE).exists():
        raise UpdateError(f"the download has no {EXE}")
    return new


def _detached() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def launch_finish(new: Path, app: Path | None = None) -> None:
    """Start the new version's exe to swap the files once this process has exited."""
    app = app or app_dir()
    subprocess.Popen([str(new / EXE), "--finish-update", str(app), "--wait-pid", str(os.getpid())],
                     cwd=str(new), creationflags=_detached(), close_fds=True)


def _start(exe: Path, *args: str) -> None:
    subprocess.Popen([str(exe), *args], cwd=str(exe.parent), creationflags=_detached(), close_fds=True)


def _message(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "VoiceRhino", 0x30)  # MB_ICONWARNING
    except Exception:
        log.warning("%s", text)


def _wait_for_exit(pid: int, timeout: float) -> None:
    import psutil

    try:
        psutil.Process(pid).wait(timeout)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired as exc:
        raise UpdateError("the running VoiceRhino didn't close") from exc


def _retry(action, attempts: int = 40, delay: float = 0.25) -> None:
    """Windows keeps a file busy for a moment after a process exits (and antivirus scans)."""
    for attempt in range(attempts):
        try:
            action()
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def finish(app: Path, wait_pid: int, source: Path | None = None,
           self_test: list[str] | None = None, start: bool = True) -> int:
    """Swap the files (runs from the new version's exe in _update). 0 = updated, 1 = rolled back."""
    source = source or Path(sys.executable).resolve().parent
    backup = app / PREVIOUS
    names = sorted({p.name for p in source.iterdir()} | APP_ITEMS)
    moved: list[str] = []  # old items now in _previous
    copied: list[str] = []  # new items now in the app folder
    try:
        if wait_pid > 0:
            _wait_for_exit(wait_pid, 60)
        if backup.exists():
            _retry(lambda: shutil.rmtree(backup))
        backup.mkdir()
        for name in names:
            if (app / name).exists():
                _retry(lambda name=name: (app / name).rename(backup / name))
                moved.append(name)
        for item in source.iterdir():
            copied.append(item.name)
            if item.is_dir():
                shutil.copytree(item, app / item.name)
            else:
                shutil.copy2(item, app / item.name)
        test = self_test or [str(app / EXE), "--self-test"]
        result = subprocess.run(test, cwd=str(app), timeout=180)
        if result.returncode != 0:
            raise UpdateError(f"the new version didn't start (self-test exit code {result.returncode})")
    except Exception as exc:
        log.exception("update failed; restoring the previous version")
        for name in copied:
            try:
                _retry(lambda name=name: _remove(app / name), attempts=8)
            except OSError:
                pass
        for name in moved:
            if (backup / name).exists():
                _retry(lambda name=name: (backup / name).rename(app / name))
        shutil.rmtree(backup, ignore_errors=True)
        _message(f"VoiceRhino couldn't update, so it went back to the version you had.\n\n{exc}")
        if start:
            _start(app / EXE)
        return 1
    shutil.rmtree(backup, ignore_errors=True)
    log.info("updated to %s", __version__)
    if start:
        _start(app / EXE, "--updated")
    return 0


def cleanup(app: Path | None = None) -> None:
    """On a normal start: remove what an update left behind (the finishing exe may still be closing)."""
    if not is_frozen():
        return
    app = app or app_dir()
    if Path(sys.executable).resolve().parent != app.resolve():
        return  # this is the copy in _update doing the swap

    def work() -> None:
        for name in (STAGING, PREVIOUS):
            path = app / name
            for _ in range(40):
                if not path.exists():
                    break
                shutil.rmtree(path, ignore_errors=True)
                time.sleep(0.5)

    threading.Thread(target=work, name="update-cleanup", daemon=True).start()
