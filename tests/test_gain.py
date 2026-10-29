"""AutoGain: boosts quiet speech, leaves normal audio, steady noise and silence alone."""

from __future__ import annotations

import numpy as np

from voicerhino.audio.file_source import read_wav
from voicerhino.audio.gain import FRAME, SILENCE_DB, AutoGain, to_db

RATE = 16000


def run(gain: AutoGain, audio: np.ndarray, block: int = 320) -> np.ndarray:
    return np.concatenate([gain.process(audio[i : i + block]) for i in range(0, len(audio), block)])


def loud_db(x: np.ndarray) -> float:
    """90th percentile of 50 ms frame levels over the non-silent frames, like AutoGain."""
    frames = x[: len(x) // FRAME * FRAME].reshape(-1, FRAME)
    levels = [to_db(float(np.mean(f * f))) for f in frames]
    return float(np.percentile([v for v in levels if v > SILENCE_DB], 90))


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x)))


def speech(conversation) -> np.ndarray:
    return read_wav(conversation[0])[0][:, 0]


def test_quiet_speech_is_boosted_to_the_target(conversation):
    quiet = speech(conversation) * 10 ** (-30 / 20)  # Discord at a few percent volume
    gain = AutoGain()
    out = run(gain, quiet)
    assert gain.gain_db > 15
    tail = out[len(out) // 2 :]  # once the gain has settled
    assert abs(loud_db(tail) - gain.target_db) < 3
    assert np.max(np.abs(out)) <= 1.0


def test_gain_is_capped(conversation):
    gain = AutoGain(max_gain_db=24)
    run(gain, speech(conversation) * 10 ** (-50 / 20))
    assert gain.gain_db == 24


def test_normal_speech_is_left_alone(conversation):
    audio = speech(conversation)
    gain = AutoGain()
    out = run(gain, audio)
    assert gain.gain_db == 0
    np.testing.assert_array_equal(out, np.clip(audio, -1, 1))


def test_steady_noise_and_silence_are_not_pumped_up():
    rng = np.random.default_rng(1)
    hiss = (rng.normal(0, 1, RATE * 10) * 10 ** (-55 / 20)).astype(np.float32)
    gain = AutoGain()
    run(gain, hiss)
    assert gain.gain_db < 1  # no speech-like swings: hold
    assert -60 < gain.loud_db < -50
    run(gain, np.zeros(RATE * 5, np.float32))
    assert gain.gain_db < 1 and gain.level_db == -140


def test_loud_sound_after_quiet_speech_backs_off_at_once(conversation):
    gain = AutoGain()
    run(gain, speech(conversation)[: RATE * 20] * 10 ** (-40 / 20))
    assert gain.gain_db > 10
    rng = np.random.default_rng(2)
    bang = rng.normal(0, 0.2, RATE // 2).astype(np.float32)  # -14 dBFS
    out = run(gain, bang)
    # The boost drops at once (not over seconds) to what keeps the bang under the ceiling.
    assert gain.gain_db <= gain.ceiling_db - to_db(0.2**2) + 0.5
    assert to_db(rms(out[2 * FRAME :]) ** 2) < gain.ceiling_db + 1


def test_disabled_measures_but_does_not_change_audio(conversation):
    quiet = speech(conversation)[: RATE * 10] * 10 ** (-30 / 20)
    gain = AutoGain(enabled=False)
    out = run(gain, quiet)
    np.testing.assert_array_equal(out, quiet)
    assert gain.gain_db > 10  # what it would apply
    assert -55 < gain.loud_db < -40


def test_gain_changes_ramp_without_steps():
    tone = (0.001 * np.sin(2 * np.pi * 200 * np.arange(RATE * 2) / RATE)).astype(np.float32)
    gain = AutoGain()
    gain.gain_db = 12.0  # pretend we just decided to boost
    out = gain.process(tone[:320])
    ratio = out / np.where(tone[:320] == 0, 1, tone[:320])
    steps = np.abs(np.diff(ratio[np.abs(tone[:320]) > 1e-4]))
    assert steps.max() < 0.1  # 1x -> 4x spread across the block, not one jump
