"""Capture one application's audio (e.g. Discord only), following it across restarts."""

from __future__ import annotations

import logging
import threading

from .base import AudioCallback, AudioSource

log = logging.getLogger(__name__)


class AppSource(AudioSource):
    """Waits for `exe_name` to run, captures its process tree, and re-attaches if
    the app restarts. Delivers nothing while the app isn't running."""

    POLL_SECONDS = 3.0

    def __init__(self, exe_name: str):
        self.exe = exe_name
        self.label = exe_name.removesuffix(".exe")
        self.attached_pid: int | None = None
        self._inner: AudioSource | None = None
        self._on_audio: AudioCallback | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_audio: AudioCallback) -> None:
        from .process_loopback import is_supported

        if not is_supported():
            raise RuntimeError("capturing a single app needs Windows 11 (or Windows 10 build 20348+)")
        self._on_audio = on_audio
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name=f"app-source-{self.label}", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        from .process_loopback import ProcessLoopbackSource, find_app_pids

        while not self._stop.is_set():
            try:
                pids = find_app_pids(self.exe)
            except Exception:
                log.exception("couldn't list processes")
                pids = []
            inner_dead = self._inner is not None and not getattr(self._inner, "running", True)
            if self.attached_pid not in pids or inner_dead:
                self._detach()
                if pids:
                    # Windows converts to 16 kHz mono for us, so the pipeline skips resampling.
                    inner = ProcessLoopbackSource(pids[0], include_tree=True, label=self.label,
                                                  sample_rate=16000, channels=1)
                    try:
                        inner.start(self._on_audio)
                        self._inner, self.attached_pid = inner, pids[0]
                        log.info("capturing %s (pid %d and children)", self.exe, pids[0])
                    except Exception:
                        log.exception("couldn't capture %s", self.exe)
            self._stop.wait(self.POLL_SECONDS)
        self._detach()

    def _detach(self) -> None:
        if self._inner is not None:
            self._inner.stop()
            log.info("stopped capturing %s", self.exe)
        self._inner, self.attached_pid = None, None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
