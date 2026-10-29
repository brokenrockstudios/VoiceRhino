"""VoiceClusterer on the synthetic 3-voice TTS conversation from tools/make_test_audio.py."""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest
import soxr

from voicerhino.speakers.voice_cluster import VoiceClusterer, ensure_speaker_model

ROOT = Path(__file__).resolve().parents[1]
STEM = ROOT / "tests" / "_out" / "conversation"
RATE = 16000


@pytest.fixture(scope="module")
def model_path() -> Path:
    try:
        return ensure_speaker_model()
    except OSError as exc:  # offline (URLError is an OSError)
        pytest.skip(f"speaker model unavailable: {exc}")


@pytest.fixture(scope="module")
def segments() -> list[tuple[str, np.ndarray]]:
    """[(true speaker, 16 kHz mono clip)] in conversation order."""
    if not (STEM.with_suffix(".wav").exists() and STEM.with_suffix(".json").exists()):
        sys.path.insert(0, str(ROOT))
        try:
            from tools.make_test_audio import build

            build(STEM)
        except (OSError, subprocess.CalledProcessError) as exc:  # no PowerShell / TTS voices
            pytest.skip(f"can't synthesize the test conversation: {exc}")
    truth = json.loads(STEM.with_suffix(".json").read_text(encoding="utf-8"))
    with wave.open(str(STEM.with_suffix(".wav")), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
    sr = truth["sample_rate"]
    return [(s["speaker"], audio[round(s["start"] * sr) : round(s["end"] * sr)]) for s in truth["segments"]]


def assert_pure(speakers: list[str], clusters: list[int | None]) -> None:
    """Every utterance assigned; each true speaker <-> exactly one cluster."""
    assert None not in clusters, list(zip(speakers, clusters))
    by_speaker: dict[str, set[int]] = defaultdict(set)
    for speaker, cluster in zip(speakers, clusters):
        by_speaker[speaker].add(cluster)
    assert all(len(c) == 1 for c in by_speaker.values()), dict(by_speaker)  # no speaker split
    assert len({next(iter(c)) for c in by_speaker.values()}) == len(by_speaker), dict(by_speaker)  # none shared


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(spectrum.size, dtype=np.float64)
    f[0] = 1.0
    x = np.fft.irfft(spectrum / np.sqrt(f), n)
    return (x / np.sqrt(np.mean(x**2))).astype(np.float32)


def test_conversation_speakers_map_one_to_one(model_path, segments):
    vc = VoiceClusterer(model_path)
    speakers = [s for s, _ in segments]
    clusters = [vc.assign(clip) for _, clip in segments]
    assert_pure(speakers, clusters)
    assert clusters[:3] == [0, 1, 2]  # ids in order of first appearance
    assert vc.num_speakers == 3
    assert vc.counts() == [3, 3, 3]
    assert vc.last is not None and vc.last.reason == "match" and vc.last.similarity >= vc.threshold
    assert vc.last.similarities.shape == (3,) and vc.last.embed_ms > 0


def test_noisy_48k_stereo_input_still_pure(model_path, segments):
    """Pink noise at -20 dB, a +-15 dB level spread, fed as 48 kHz stereo."""
    rng = np.random.default_rng(0)
    vc = VoiceClusterer(model_path)
    clusters = []
    for _, clip in segments:
        noisy = clip + pink_noise(len(clip), rng) * np.sqrt(np.mean(clip**2)) * 0.1
        noisy *= 10 ** (rng.uniform(-15, 15) / 20)
        x48 = soxr.resample(noisy, RATE, 48000).astype(np.float32)
        clusters.append(vc.assign(np.stack([x48, x48], axis=1), 48000))
    assert_pure([s for s, _ in segments], clusters)
    assert vc.num_speakers == 3


def test_too_short_or_silent_returns_none(model_path, segments):
    vc = VoiceClusterer(model_path)
    clip = segments[0][1]
    assert vc.embed(clip[: int(0.3 * RATE)]) is None
    assert vc.assign(clip[: int(0.3 * RATE)]) is None
    assert vc.last.reason == "short"
    assert vc.assign(np.zeros(2 * RATE, np.float32)) is None  # digital silence
    assert vc.num_speakers == 0
    embedding = vc.embed(clip)
    assert embedding is not None and embedding.shape == (vc.dim,)
    assert np.linalg.norm(embedding) == pytest.approx(1.0, abs=1e-4)


def test_short_clip_joins_but_never_opens_a_cluster(model_path, segments):
    vc = VoiceClusterer(model_path)
    one_second = [(s, clip[len(clip) // 2 - RATE // 2 :][:RATE]) for s, clip in segments]
    assert vc.assign(one_second[0][1]) is None  # nothing to join yet
    assert vc.last.reason == "unsure" and vc.num_speakers == 0

    clusters = {}
    for speaker, clip in segments[:3]:
        clusters[speaker] = vc.assign(clip)
    counts = vc.counts()
    joined = 0
    for speaker, clip in one_second:
        got = vc.assign(clip)
        assert got in (clusters[speaker], None)  # the right speaker, or "don't know"
        joined += got is not None
    assert joined >= len(one_second) - 2  # measured: all 9 join (cosine 0.61-0.80)
    assert vc.num_speakers == 3
    assert vc.counts() == counts  # short clips don't move centroids


def test_names_and_reset(model_path, segments):
    vc = VoiceClusterer(model_path)
    for _, clip in segments[:3]:
        vc.assign(clip)
    vc.set_name(1, "zira#1234")
    assert vc.name(1) == "zira#1234" and vc.name(0) is None
    vc.set_name(1, None)
    assert vc.name(1) is None
    with pytest.raises(KeyError):
        vc.set_name(3, "nobody")
    vc.set_name(0, "mark")

    vc.reset()
    assert vc.num_speakers == 0 and vc.last is None and vc.name(0) is None
    assert vc.assign(segments[2][1]) == 0  # ids restart at 0


def test_max_speakers_caps_clusters(model_path, segments):
    vc = VoiceClusterer(model_path, max_speakers=2)
    clusters, reasons = [], []
    for _, clip in segments:
        clusters.append(vc.assign(clip))
        reasons.append(vc.last.reason)
    assert None not in clusters and set(clusters) == {0, 1}
    assert vc.num_speakers == 2
    assert reasons[2] == "nearest"  # the third voice has nowhere to go


def test_unknown_model_name():
    with pytest.raises(ValueError):
        ensure_speaker_model("no-such-model")
