"""Capture from a WASAPI input or from the loopback of any output device."""

from __future__ import annotations

import logging

import numpy as np

from .base import AudioCallback, AudioSource
from .devices import DEFAULT_OUTPUT_KEY, resolve

log = logging.getLogger(__name__)


class WasapiSource(AudioSource):
    def __init__(self, device_key: str = DEFAULT_OUTPUT_KEY):
        self.device_key = device_key
        self.label = device_key.split(":", 1)[-1]
        self.device_name: str | None = None  # the device actually opened
        self._pa = None
        self._stream = None

    def alive(self) -> bool:
        """False once the stream has died (device unplugged, driver reset)."""
        try:
            return self._stream is not None and self._stream.is_active()
        except Exception:
            return False

    def stale(self) -> bool:
        """True if this source should reopen: dead, or following the default
        output and Windows switched it to another device."""
        if not self.alive():
            return True
        if self.device_key != DEFAULT_OUTPUT_KEY:
            return False
        try:
            return resolve(DEFAULT_OUTPUT_KEY).name != self.device_name
        except LookupError:
            return False

    def start(self, on_audio: AudioCallback) -> None:
        import pyaudiowpatch as pyaudio

        device = resolve(self.device_key)
        self.label = device.label
        self.device_name = device.name
        channels, rate = device.channels, device.rate

        def callback(in_data, frame_count, time_info, status):
            block = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels)
            on_audio(block, rate)
            return (None, pyaudio.paContinue)

        self._pa = pyaudio.PyAudio()
        try:
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device.index,
                frames_per_buffer=rate // 50,  # 20 ms blocks
                stream_callback=callback,
            )
        except Exception:
            self.stop()
            raise
        log.info("capturing %s (%d ch @ %d Hz)", device.label, channels, rate)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                log.debug("error closing stream", exc_info=True)
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
