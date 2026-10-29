"""Automatic gain for quiet sources, and the input level it measures.

A deaf user can't hear that the audio has gone quiet: per-app capture follows
the Volume Mixer, and Discord's own output volume scales everything we capture.
The voice detector and the streaming live model both miss quiet speech, so
AutoGain slowly boosts (never cuts) audio whose loud parts sit below a target
level. It also keeps the levels the Settings meter and the "audio is very
quiet" warning show.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

FRAME = 800  # 50 ms at 16 kHz
SILENCE_DB = -90.0  # frames below this are silence (idle loopback sends zeros): they never change the gain
MIN_DYNAMICS_DB = 10.0  # speech swings this much between syllables and pauses; steady hum doesn't


def to_db(power: float) -> float:
    return 10 * math.log10(power) if power > 1e-14 else -140.0


class AutoGain:
    """Boost toward `target_db`, measured as the 90th percentile of recent 50 ms
    frame levels (the loud parts: speech peaks rather than the pauses between words).

    The gain rises slowly (`rise_db_per_s`), only while the sound has speech-like
    swings (a steady hum or hiss is never pumped up), falls fast, and drops at
    once if a boosted frame would get loud.
    """

    def __init__(self, enabled: bool = True, target_db: float = -26.0, max_gain_db: float = 24.0,
                 window_s: float = 6.0, rise_db_per_s: float = 8.0, fall_db_per_s: float = 40.0,
                 ceiling_db: float = -10.0, rate: int = 16000):
        self.enabled = enabled
        self.target_db = target_db
        self.max_gain_db = max_gain_db
        self.ceiling_db = ceiling_db  # boosted 50 ms RMS never above this
        self._rise = rise_db_per_s * FRAME / rate  # dB per frame
        self._fall = fall_db_per_s * FRAME / rate
        self._levels: deque[float] = deque(maxlen=max(1, int(window_s * rate / FRAME)))
        self._sum_sq = 0.0
        self._count = 0
        self.gain_db = 0.0  # applied to the audio
        self._applied = 1.0  # linear gain at the end of the previous block (ramps from here)
        self.level_db = -140.0  # input (pre-gain) level of the last 50 ms frame
        self.loud_db = -140.0  # input level of the loud parts over the window; -140 = no sound yet

    @property
    def gain(self) -> float:
        return 10 ** (self.gain_db / 20)

    def process(self, x: np.ndarray) -> np.ndarray:
        """Measure `x` (16 kHz mono float32) and return it with the gain applied."""
        if x.size == 0:
            return x
        out = np.empty_like(x)
        pos = 0
        while pos < x.size:
            take = min(FRAME - self._count, x.size - pos)
            chunk = x[pos : pos + take]
            target = self.gain if self.enabled else 1.0
            if target == self._applied:
                out[pos : pos + take] = chunk * target
            else:  # ramp across the chunk so gain changes don't click
                ramp = np.linspace(self._applied, target, take + 1, dtype=np.float32)[1:]
                out[pos : pos + take] = chunk * ramp
            self._applied = target
            self._sum_sq += float(np.dot(chunk, chunk))
            self._count += take
            pos += take
            if self._count == FRAME:
                self._on_frame(self._sum_sq / FRAME)
                self._sum_sq, self._count = 0.0, 0
        np.clip(out, -1.0, 1.0, out=out)
        return out

    def _on_frame(self, power: float) -> None:
        level = to_db(power)
        self.level_db = level
        if level < SILENCE_DB:
            return  # silence (or a source that sends nothing): hold the gain
        self._levels.append(level)
        low, self.loud_db = (float(v) for v in np.percentile(self._levels, [10, 90]))
        if len(self._levels) < 6:  # under 0.3 s of sound: too little to act on
            return
        want = min(max(self.target_db - self.loud_db, 0.0), self.max_gain_db)
        if want > self.gain_db and self.loud_db - low >= MIN_DYNAMICS_DB:
            self.gain_db = min(want, self.gain_db + self._rise)
        elif want < self.gain_db:
            self.gain_db = max(want, self.gain_db - self._fall)
        # A boosted frame must never get loud (someone unmutes, an explosion): back off at once.
        self.gain_db = min(self.gain_db, max(self.ceiling_db - level, 0.0))
