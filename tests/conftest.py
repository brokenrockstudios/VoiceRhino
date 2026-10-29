from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "_out"


def word_error_rate(reference: str, hypothesis: str) -> float:
    norm = lambda s: [w.strip(".,?!'\"").lower() for w in s.split() if w.strip(".,?!'\"")]
    ref, hyp = norm(reference), norm(hypothesis)
    # number words: the recognizer writes "10", the script says "ten"
    digits = {"ten": "10", "thirty": "30"}
    ref = [digits.get(w, w) for w in ref]
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)] / max(1, len(ref))


@pytest.fixture(scope="session")
def engine():
    """The default recognizer (skips if it hasn't been downloaded)."""
    from voicerhino.asr.catalog import DEFAULT_MODEL_ID, get_model
    from voicerhino.asr.download import is_downloaded
    from voicerhino.asr.engines import EngineOptions, load_engine

    spec = get_model(DEFAULT_MODEL_ID)
    if not is_downloaded(spec.source):
        pytest.skip("default model not downloaded")
    return load_engine(spec, EngineOptions(num_threads=2))


@pytest.fixture(scope="session")
def conversation() -> tuple[Path, dict]:
    """Synthetic 3-voice conversation (generated once with Windows TTS)."""
    wav = OUT / "conversation.wav"
    if not wav.exists():
        sys.path.insert(0, str(ROOT))
        from tools.make_test_audio import build

        build(OUT / "conversation")
    return wav, json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
