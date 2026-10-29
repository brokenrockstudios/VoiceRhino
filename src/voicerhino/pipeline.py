"""Audio -> VAD -> recognizer -> Utterance events.

CaptionProcessor is the pure core: it's driven only by the samples fed to it,
so tests can run it faster than real time. CaptionPipeline wraps it in a
thread fed by a live AudioSource, resamples to 16 kHz mono and fills gaps
(loopback devices send nothing while nothing plays) so utterances still end.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .asr.engines import SAMPLE_RATE, Engine, StreamingEngine
from .audio.base import AudioSource
from .audio.gain import SILENCE_DB, AutoGain
from .transcript import Utterance

log = logging.getLogger(__name__)

# speaker_fn(audio_16k, start, end) -> (speaker_key, display_name) or None
SpeakerFn = Callable[[np.ndarray, float, float], "tuple[str, str] | None"]
Emit = Callable[[Utterance], None]

_ids = itertools.count(1)

# Spoken lines quieter than this (90th percentile of 50 ms levels, before the boost) get the
# "audio is very quiet" warning: the boost is maxed out and still short. On the TTS test the
# live text held up to about here and fell apart ~10 dB lower.
QUIET_DB = -55.0


@dataclass
class PipelineSettings:
    vad_threshold: float = 0.5
    min_silence: float = 0.5  # seconds of quiet that end an utterance
    # Shortest speech the VAD acts on. 0.25 s dropped a short word before a pause ("Wait,
    # their jungler..." lost "Wait") and isn't needed to reject game noise.
    min_speech: float = 0.1
    max_utterance: float = 12.0  # force a final this often during long monologues
    partial_interval: float = 0.4  # live re-decode cadence while speaking; 0 disables
    pre_roll: float = 0.25  # audio kept before the VAD's start: it tends to clip first words
    post_roll: float = 0.05
    auto_gain: bool = True  # boost quiet audio (see audio/gain.py)


@dataclass(frozen=True)
class SourceLevel:
    """How one running source sounds right now (for the Settings meter and the quiet warning)."""

    name: str
    level_db: float  # input level of the last 50 ms, before any boost; -140 = silence
    gain_db: float  # boost being applied
    speech_db: float | None  # recent spoken lines before the boost; None = nobody spoke lately


class AudioHistory:
    """The last N seconds of 16 kHz audio, addressed by absolute sample index."""

    def __init__(self, seconds: float):
        self._cap = int(seconds * SAMPLE_RATE)
        self._buf = np.zeros(self._cap, np.float32)
        self.end = 0  # absolute index one past the newest sample

    @property
    def start(self) -> int:
        return max(0, self.end - self._cap)

    def append(self, x: np.ndarray) -> None:
        if len(x) > self._cap:
            self.end += len(x) - self._cap
            x = x[-self._cap :]
        idx = np.arange(self.end, self.end + len(x)) % self._cap
        self._buf[idx] = x
        self.end += len(x)

    def slice(self, a: int, b: int) -> np.ndarray:
        a, b = max(a, self.start), min(b, self.end)
        if b <= a:
            return np.zeros(0, np.float32)
        return self._buf[np.arange(a, b) % self._cap]


def make_vad(model_path: Path, settings: PipelineSettings):
    import sherpa_onnx as so

    config = so.VadModelConfig()
    config.silero_vad.model = str(model_path)
    config.silero_vad.threshold = settings.vad_threshold
    config.silero_vad.min_silence_duration = settings.min_silence
    config.silero_vad.min_speech_duration = settings.min_speech
    config.silero_vad.max_speech_duration = settings.max_utterance
    config.sample_rate = SAMPLE_RATE
    config.num_threads = 1
    return so.VoiceActivityDetector(config, buffer_size_in_seconds=settings.max_utterance + 30)


class CaptionProcessor:
    def __init__(
        self,
        engine: Engine,
        vad_model: Path,
        settings: PipelineSettings,
        emit: Emit,
        source: str = "main",
        start_time: float | None = None,
        speaker_fn: SpeakerFn | None = None,
        live_engine: StreamingEngine | None = None,
        source_id: str = "",
    ):
        self.engine = engine
        self.live_engine = live_engine  # set: two-pass (streaming live line, `engine` finals)
        self.settings = settings
        self.emit = emit
        self.source = source  # display name
        self.source_id = source_id  # SourceConfig.id, for per-window source choices
        self.speaker_fn = speaker_fn
        self.start_time = time.monotonic() if start_time is None else start_time
        self.gain = AutoGain(enabled=settings.auto_gain)
        self._vad = make_vad(vad_model, settings)
        self._audio = AudioHistory(settings.max_utterance + 20)
        self._speech_levels: deque[float] = deque(maxlen=3)  # input level of recent spoken lines
        self._spoke_at = 0  # sample index of the last spoken line
        self._utt_id: int | None = None
        self._next_partial = 0  # sample index when the next partial is due
        self._stream = None  # live_engine stream for the utterance in progress
        self._stream_pos = 0  # sample index fed to it so far
        self._stream_start = 0
        self._stream_text = ""

    @property
    def total_samples(self) -> int:
        return self._audio.end

    def time_at(self, sample_index: int) -> float:
        return self.start_time + sample_index / SAMPLE_RATE

    @property
    def speech_db(self) -> float | None:
        """How loud recent spoken lines were before the boost (dB), or None when
        nobody has spoken in the last two minutes."""
        levels = tuple(self._speech_levels)  # one C call: safe against the pipeline thread appending
        if not levels or self._audio.end - self._spoke_at > 120 * SAMPLE_RATE:
            return None
        return float(np.median(levels))

    def feed(self, samples: np.ndarray) -> None:
        samples = self.gain.process(np.asarray(samples, dtype=np.float32))
        self._audio.append(samples)
        self._vad.accept_waveform(samples)
        self._drain_finals()
        if self._vad.is_speech_detected():
            if self.live_engine is not None:
                self._stream_partial()
            else:
                self._maybe_partial()
        elif self._utt_id is not None:
            # Speech was detected but the VAD dropped it (too short): clear the live line.
            self._emit(self._utt_id, "", self._audio.end, self._audio.end, final=True)
            self._utt_id = None
            self._stream = None

    def flush(self) -> None:
        """End any in-progress utterance (on stop)."""
        self._vad.flush()
        self._drain_finals()

    def _maybe_partial(self) -> None:
        s = self.settings
        if s.partial_interval <= 0 or self._audio.end < self._next_partial:
            return
        segment = self._vad.current_segment
        start = segment.start - int(s.pre_roll * SAMPLE_RATE)
        if self._utt_id is None:
            self._utt_id = next(_ids)
        audio = self._audio.slice(start, self._audio.end)
        if len(audio) < 0.3 * SAMPLE_RATE:
            return
        t = time.perf_counter()
        text = self.engine.transcribe(audio)
        cost = time.perf_counter() - t
        self._emit(self._utt_id, text, start, self._audio.end, final=False, decode_s=cost,
                   speaker=self._live_speaker(start, self._audio.end))
        # Never spend more than ~half the time decoding partials.
        wait = max(s.partial_interval, 2 * cost)
        self._next_partial = self._audio.end + int(wait * SAMPLE_RATE)

    def _stream_partial(self) -> None:
        """Two-pass live line: feed new audio to the streaming model, show its text."""
        if self._stream is None:
            start = max(self._vad.current_segment.start - int(self.settings.pre_roll * SAMPLE_RATE),
                        self._audio.start)
            self._stream = self.live_engine.new_stream()
            self._stream_pos = self._stream_start = start
            self._stream_text = ""
            if self._utt_id is None:
                self._utt_id = next(_ids)
        audio = self._audio.slice(self._stream_pos, self._audio.end)
        self._stream_pos = self._audio.end
        if audio.size == 0:
            return
        t = time.perf_counter()
        text = self.live_engine.feed(self._stream, audio)
        cost = time.perf_counter() - t
        if text and text != self._stream_text:
            self._stream_text = text
            self._emit(self._utt_id, text, self._stream_start, self._audio.end, final=False, decode_s=cost,
                       speaker=self._live_speaker(self._stream_start, self._audio.end))

    def _live_speaker(self, start: int, end: int) -> tuple[str, str] | None:
        """Cheap attribution (e.g. Discord's speaking events) for a partial line."""
        live = getattr(self.speaker_fn, "live", None)
        if live is None:
            return None
        try:
            return live(self.time_at(start), self.time_at(end))
        except Exception:
            log.exception("live speaker attribution failed")
            return None

    def _drain_finals(self) -> None:
        s = self.settings
        while not self._vad.empty():
            segment = self._vad.front
            start = segment.start - int(s.pre_roll * SAMPLE_RATE)
            end = segment.start + len(segment.samples) + int(s.post_roll * SAMPLE_RATE)
            self._vad.pop()
            audio = self._audio.slice(start, end)
            t = time.perf_counter()
            text = self.engine.transcribe(audio)
            cost = time.perf_counter() - t
            speaker = None
            if text and self.speaker_fn is not None:
                try:
                    speaker = self.speaker_fn(audio, self.time_at(start), self.time_at(end))
                except Exception:
                    log.exception("speaker attribution failed")
            if text and self.gain.loud_db > SILENCE_DB:
                # The gain's 6 s window is mostly this line, so its loud level is the speaker's.
                self._speech_levels.append(self.gain.loud_db)
                self._spoke_at = self._audio.end
            utt_id = self._utt_id if self._utt_id is not None else next(_ids)
            self._utt_id = None
            self._next_partial = 0
            self._stream = None  # the next utterance gets a fresh live stream
            self._emit(utt_id, text, start, end, final=True, decode_s=cost, speaker=speaker)

    def _emit(self, utt_id: int, text: str, start: int, end: int, final: bool,
              decode_s: float = 0.0, speaker: tuple[str, str] | None = None) -> None:
        self.emit(
            Utterance(
                id=utt_id,
                source=self.source,
                text=text,
                start=self.time_at(start),
                end=self.time_at(end),
                final=final,
                speaker_key=speaker[0] if speaker else None,
                speaker=speaker[1] if speaker else None,
                decode_ms=decode_s * 1000,
                source_id=self.source_id,
            )
        )


class _Resampler:
    """Downmix to mono and resample to 16 kHz, rebuilding if the device rate changes."""

    def __init__(self):
        self._rate = None
        self._stream = None

    def __call__(self, block: np.ndarray, rate: int) -> np.ndarray:
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        if rate == SAMPLE_RATE:
            return mono
        if rate != self._rate:
            import soxr

            self._stream = soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32", quality="HQ")
            self._rate = rate
        return self._stream.resample_chunk(mono)


class CaptionPipeline:
    """Runs one AudioSource through a CaptionProcessor on a worker thread."""

    GAP_FILL_AFTER = 0.25  # seconds without audio before we assume silence

    def __init__(self, source: AudioSource, engine: Engine, vad_model: Path,
                 settings: PipelineSettings, emit: Emit, source_name: str = "main",
                 speaker_fn: SpeakerFn | None = None, live_engine: StreamingEngine | None = None,
                 source_id: str = ""):
        self.source = source
        self._factory = lambda t0: CaptionProcessor(
            engine, vad_model, settings, emit, source_name, t0, speaker_fn, live_engine, source_id
        )
        self._queue: queue.Queue[tuple[np.ndarray, int]] = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.processor: CaptionProcessor | None = None
        self.level = 0.0  # recent input RMS, for a meter in the UI
        self.error: str | None = None

    def start(self) -> None:
        """Start the worker, then the source. If the source fails to open this
        raises, but the worker keeps running so restart_source() can retry."""
        self._stop.clear()
        t0 = time.monotonic()
        self.processor = self._factory(t0)
        self._thread = threading.Thread(target=self._run, args=(t0,), name="caption-pipeline", daemon=True)
        self._thread.start()
        self.source.start(self._on_audio)

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def level_info(self) -> SourceLevel | None:
        proc = self.processor
        if proc is None:
            return None
        gain = proc.gain
        return SourceLevel(proc.source, gain.level_db, gain.gain_db if gain.enabled else 0.0, proc.speech_db)

    def restart_source(self) -> bool:
        """Reopen the audio source (after an unplug or a default-device switch).
        The processor keeps running and fills the gap with silence."""
        self.source.stop()
        try:
            self.source.start(self._on_audio)
            return True
        except Exception as exc:
            log.info("source not available yet: %s", exc)
            return False

    def _on_audio(self, block: np.ndarray, rate: int) -> None:
        try:
            self._queue.put_nowait((block.copy(), rate))
        except queue.Full:
            log.warning("audio queue full; dropping %d frames", len(block))

    def _run(self, t0: float) -> None:
        resample = _Resampler()
        proc = self.processor
        assert proc is not None
        try:
            while not self._stop.is_set():
                try:
                    block, rate = self._queue.get(timeout=0.05)
                except queue.Empty:
                    pass
                else:
                    x = resample(block, rate)
                    if x.size:
                        self.level = float(np.sqrt(np.mean(x * x)))
                        proc.feed(x)
                    continue
                # No audio arrived: keep the timeline in step with the clock so
                # the VAD sees the silence that ends an utterance.
                behind = (time.monotonic() - t0) * SAMPLE_RATE - proc.total_samples
                if behind > self.GAP_FILL_AFTER * SAMPLE_RATE:
                    self.level = 0.0
                    proc.feed(np.zeros(int(behind - 0.05 * SAMPLE_RATE), np.float32))
            proc.flush()
        except Exception as exc:  # surface in the UI instead of dying silently
            log.exception("caption pipeline crashed")
            self.error = str(exc)
