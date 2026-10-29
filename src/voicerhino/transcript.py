"""Caption events and the shared, thread-safe record of recent ones."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable


@dataclass(frozen=True)
class Utterance:
    """One stretch of speech. Partials and the final share an id, so views can
    replace the live line in place when it's finalized."""

    id: int
    source: str  # which capture source produced it
    text: str
    start: float  # time.monotonic()
    end: float
    final: bool
    speaker_key: str | None = None  # stable color key, e.g. "discord:1234" or "voice:2"
    speaker: str | None = None  # display name, e.g. "Ryan" or "Speaker 3"
    translation: str | None = None
    decode_ms: float = 0.0
    source_id: str = ""  # the SourceConfig id, so each window can choose which sources it shows

    @property
    def wall_start(self) -> float:
        """time.time() equivalent of start, for clock display."""
        return time.time() - (time.monotonic() - self.start)


Listener = Callable[[Utterance], None]


@dataclass
class TranscriptStore:
    """Keeps finalized utterances for `keep_seconds` plus the live partial per source.

    push() is called from pipeline threads; listeners run on the pushing thread,
    so UI listeners must hop to the UI thread themselves (see ui/bridge.py).
    """

    keep_seconds: float = 15 * 60
    _finals: deque[Utterance] = field(default_factory=deque)
    _partials: dict[str, Utterance] = field(default_factory=dict)
    _listeners: list[Listener] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def push(self, utt: Utterance) -> None:
        key = utt.source_id or utt.source  # two sources may share a display name
        with self._lock:
            current = self._partials.get(key)
            if utt.final:
                if current is not None and current.id == utt.id:
                    del self._partials[key]
                if utt.text:
                    self._finals.append(utt)
                cutoff = time.monotonic() - self.keep_seconds
                while self._finals and self._finals[0].end < cutoff:
                    self._finals.popleft()
            elif utt.text:
                self._partials[key] = utt
        for listener in list(self._listeners):
            listener(utt)

    def update(self, utt_id: int, **changes) -> Utterance | None:
        """Amend a finalized utterance (late translation or speaker name)."""
        with self._lock:
            for i, old in enumerate(self._finals):
                if old.id == utt_id:
                    new = replace(old, **changes)
                    self._finals[i] = new
                    break
            else:
                return None
        for listener in list(self._listeners):
            listener(new)
        return new

    def finals(self, since: float | None = None) -> list[Utterance]:
        with self._lock:
            if since is None:
                return list(self._finals)
            return [u for u in self._finals if u.end >= since]

    def partials(self) -> list[Utterance]:
        with self._lock:
            return sorted(self._partials.values(), key=lambda u: u.start)

    def clear(self) -> None:
        with self._lock:
            self._finals.clear()
            self._partials.clear()
