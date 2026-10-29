"""Play a WAV file into the pipeline as if it were live audio (for demos and tests)."""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

from .base import AudioCallback, AudioSource


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Return float32 samples shaped (frames, channels) and the sample rate."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    return data.reshape(-1, channels), rate


class FileSource(AudioSource):
    def __init__(self, path: Path, realtime: bool = True, block_seconds: float = 0.02, loop: bool = False):
        self.path = Path(path)
        self.label = self.path.name
        self.realtime = realtime
        self.block_seconds = block_seconds
        self.loop = loop
        self.done = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_audio: AudioCallback) -> None:
        data, rate = read_wav(self.path)
        step = max(1, int(rate * self.block_seconds))

        def run() -> None:
            t0 = time.monotonic()
            sent = 0
            while not self._stop.is_set():
                for i in range(0, len(data), step):
                    if self._stop.is_set():
                        break
                    on_audio(data[i : i + step], rate)
                    sent += len(data[i : i + step])
                    if self.realtime:
                        delay = t0 + sent / rate - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                if not self.loop:
                    break
            self.done.set()

        self._stop.clear()
        self.done.clear()
        self._thread = threading.Thread(target=run, name="file-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
