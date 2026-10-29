from __future__ import annotations

import time

import numpy as np
import pytest

from conftest import word_error_rate
from voicerhino.asr.catalog import VAD_MODELS, get_model
from voicerhino.asr.download import ensure_download, is_downloaded
from voicerhino.audio.file_source import FileSource, read_wav
from voicerhino.pipeline import QUIET_DB, AudioHistory, CaptionPipeline, CaptionProcessor, PipelineSettings
from voicerhino.transcript import TranscriptStore

MODEL = get_model("parakeet-tdt-0.6b-v2-int8")
needs_model = pytest.mark.skipif(not is_downloaded(MODEL.source), reason="Parakeet v2 not downloaded")


def test_audio_history_wraps():
    h = AudioHistory(1.0)  # 16000 samples
    h.append(np.arange(10000, dtype=np.float32))
    h.append(np.arange(10000, 20000, dtype=np.float32))
    assert h.start == 4000 and h.end == 20000
    np.testing.assert_array_equal(h.slice(15990, 16010), np.arange(15990, 16010))
    assert h.slice(0, 100).size == 0  # evicted


@needs_model
def test_processor_transcribes_conversation(engine, conversation):
    wav, truth = conversation
    data, rate = read_wav(wav)
    events = []
    proc = CaptionProcessor(engine, ensure_download(VAD_MODELS["silero"]), PipelineSettings(),
                            lambda u: events.append((proc.total_samples / rate, u)), start_time=0.0)
    for i in range(0, len(data), 320):  # 20 ms blocks
        proc.feed(data[i : i + 320, 0])
    proc.flush()

    finals = [u for _, u in events if u.final and u.text]
    partials = [u for _, u in events if not u.final]
    hypothesis = " ".join(u.text for u in finals)
    reference = " ".join(s["text"] for s in truth["segments"])
    wer = word_error_rate(reference, hypothesis)
    print(f"\nWER {wer:.1%}\n{hypothesis}")
    assert wer < 0.08
    # A short word before a pause used to fall under the VAD's minimum speech length.
    assert "wait, their jungler" in hypothesis.lower()
    assert partials, "expected live partial updates while speaking"
    # Every partial must belong to an utterance that is later finalized (or cleared).
    final_ids = {u.id for _, u in events if u.final}
    assert {u.id for u in partials} <= final_ids

    # Caption latency, in audio time: from the end of each spoken line to the
    # moment its last words are on screen as a final.
    lags = []
    for seg in truth["segments"]:
        done = [t for t, u in events if u.final and u.text and seg["start"] - 0.3 <= u.start <= seg["end"]]
        if done:
            lags.append(max(done) - seg["end"])
    print(f"final-caption lag after speech ends: median {np.median(lags):.2f}s, max {max(lags):.2f}s")
    assert np.median(lags) < 1.0


@needs_model
def test_live_pipeline_with_gaps(engine, conversation):
    """Real-time file playback with a long pause, through the threaded pipeline:
    the final must arrive even though no audio is delivered during the gap."""
    wav, _ = conversation
    store = TranscriptStore()
    source = FileSource(wav, realtime=True)
    data, rate = read_wav(wav)
    # Only play the first spoken line (about 5 s) then go quiet like an idle loopback.
    source.start = _first_seconds(source, data, rate, 5.0)
    pipe = CaptionPipeline(source, engine, ensure_download(VAD_MODELS["silero"]), PipelineSettings(), store.push)
    t0 = time.monotonic()
    pipe.start()
    deadline = t0 + 12
    while time.monotonic() < deadline and not store.finals():
        time.sleep(0.05)
    pipe.stop()
    finals = store.finals()
    assert finals, f"no final caption (pipeline error: {pipe.error})"
    assert "top lane" in finals[0].text.lower()


@needs_model
def test_quiet_audio_is_boosted_and_measured(engine, conversation):
    """Discord turned far down: the boost keeps finals accurate, and speech_db (what the
    quiet warning uses) reports how loud the speakers really were, before the boost."""
    wav, truth = conversation
    data, _ = read_wav(wav)
    reference = " ".join(s["text"] for s in truth["segments"])
    vad = ensure_download(VAD_MODELS["silero"])
    measured = {}
    for gain_db in (-25, -45):  # the TTS voices peak at about -16 dBFS (90th percentile)
        events = []
        proc = CaptionProcessor(engine, vad, PipelineSettings(partial_interval=0), events.append, start_time=0.0)
        audio = data[:, 0] * np.float32(10 ** (gain_db / 20))
        for i in range(0, len(audio), 320):
            proc.feed(audio[i : i + 320])
        proc.flush()
        hypothesis = " ".join(u.text for u in events if u.final and u.text)
        measured[gain_db] = (word_error_rate(reference, hypothesis), proc.speech_db, proc.gain.gain_db)
    wer, speech_db, boost = measured[-25]
    assert wer < 0.08 and boost > 10
    assert -46 < speech_db < -36 and speech_db > QUIET_DB  # boosted quietly, no warning
    wer, speech_db, boost = measured[-45]
    assert boost == 24  # as far as it goes
    assert -66 < speech_db < -56 and speech_db < QUIET_DB  # the overlays warn
    assert wer < 0.2


def _first_seconds(source, data, rate, seconds):
    clip = data[: int(seconds * rate)]

    def start(on_audio):
        import threading

        def run():
            for i in range(0, len(clip), rate // 50):
                on_audio(clip[i : i + rate // 50], rate)
                time.sleep(0.02)
            # then nothing at all, like a loopback endpoint with nothing playing

        threading.Thread(target=run, daemon=True).start()

    return start


LIVE = get_model("nemotron-streaming-en-160ms-int8")


@pytest.mark.skipif(not is_downloaded(LIVE.source), reason="Nemotron streaming model not downloaded")
def test_two_pass_live_text_grows_and_finals_stay_accurate(engine, conversation):
    from voicerhino.asr.engines import load_streaming_engine

    wav, truth = conversation
    data, _ = read_wav(wav)
    events = []
    proc = CaptionProcessor(engine, ensure_download(VAD_MODELS["silero"]), PipelineSettings(),
                            events.append, start_time=0.0, live_engine=load_streaming_engine(LIVE))
    for i in range(0, len(data), 320):
        proc.feed(data[i : i + 320, 0])
    proc.flush()

    finals = [u for u in events if u.final and u.text]
    reference = " ".join(s["text"] for s in truth["segments"])
    assert word_error_rate(reference, " ".join(u.text for u in finals)) < 0.08  # finals: main model

    partials = [u for u in events if not u.final]
    assert len(partials) > 2 * len(finals), "expected word-by-word updates"
    # The live line should only grow: each update keeps the previous one's words.
    pairs = grows = 0
    for prev, cur in zip(partials, partials[1:]):
        if prev.id == cur.id:
            pairs += 1
            p, c = prev.text.split(), cur.text.split()
            grows += c[: len(p) - 1] == p[:-1]  # the last word may still be completing ("drag" -> "dragon")
    assert grows >= 0.9 * pairs


class _FakeEngine:
    def transcribe(self, samples):
        return "hello"


def test_utterances_carry_their_source_id(conversation):
    """Windows filter captions by source id (Settings › Windows › Captions from)."""
    wav, _ = conversation
    data, _ = read_wav(wav)
    events = []
    proc = CaptionProcessor(_FakeEngine(), ensure_download(VAD_MODELS["silero"]), PipelineSettings(),
                            events.append, source="Microphone", start_time=0.0, source_id="mic01")
    for i in range(0, 16000 * 6, 320):
        proc.feed(data[i : i + 320, 0])
    proc.flush()
    assert events and {(u.source, u.source_id) for u in events} == {("Microphone", "mic01")}
