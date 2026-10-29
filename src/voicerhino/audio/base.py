"""Audio source interface shared by every capture backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

import numpy as np

# Invoked from the capture thread with float32 samples shaped (frames, channels)
# and the block's sample rate. Must return quickly: copy and hand off, never block.
AudioCallback = Callable[[np.ndarray, int], None]


class AudioSource(ABC):
    """A stream of audio delivered to a callback on a background thread.

    Sources only deliver what the OS gives them. Loopback endpoints deliver
    nothing at all while no app is playing; the pipeline fills those gaps with
    silence based on wall-clock time, so sources don't need to.
    """

    #: Label for the UI, e.g. "VOIPSpeakers (Realtek USB2.0 Audio) [loopback]".
    label: str = ""

    @abstractmethod
    def start(self, on_audio: AudioCallback) -> None:
        """Begin capturing; on_audio is called from a non-UI thread."""

    @abstractmethod
    def stop(self) -> None:
        """Stop capturing and release the device. Safe to call more than once."""
