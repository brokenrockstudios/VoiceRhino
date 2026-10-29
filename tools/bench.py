"""Compare models on the synthetic conversation: accuracy, latency and load.

Replays the audio through the real CaptionProcessor (live partials included),
as fast as possible, and reports how much compute captioning actually costs.

    python tools/bench.py parakeet-tdt-0.6b-v2-int8 fw-large-v3-turbo:cuda
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from conftest import word_error_rate  # noqa: E402
from voicerhino.asr.catalog import VAD_MODELS, get_model  # noqa: E402
from voicerhino.asr.download import ensure_download  # noqa: E402
from voicerhino.asr.engines import EngineOptions, load_engine, load_streaming_engine  # noqa: E402
from voicerhino.audio.file_source import read_wav  # noqa: E402
from voicerhino.pipeline import CaptionProcessor, PipelineSettings  # noqa: E402


def gpu_memory_used_mb() -> float | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return float(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _words(text: str) -> list[str]:
    return [w.strip(".,?!'\"").lower() for w in text.split() if w.strip(".,?!'\"")]


def _starts_with(text: str, word: str) -> bool:
    """Does the live text begin with `word`? Tolerates "All right" for "Alright"."""
    have = _words(text)
    return bool(have) and any(
        difflib.SequenceMatcher(None, "".join(have[:n]), word).ratio() >= 0.85 for n in (1, 2))


def live_metrics(stamped: list, truth: dict) -> dict:
    """Medians over the spoken lines: seconds from speech start to the first live text,
    and to a live line that starts with the right word (lines where that never
    happened are counted in first_wrong, lines with no captions at all in
    lines_missed), plus the share of a line's words already on screen 150 ms after it ends."""
    first, correct, shown, wrong, missed = [], [], [], 0, 0
    for seg in truth["segments"]:
        mine = [(t, u) for t, u in stamped if u.text and seg["start"] - 0.3 <= u.start <= seg["end"]]
        if not mine:
            missed += 1
            continue
        first.append(min(t for t, _ in mine) - seg["start"])
        right = [t for t, u in mine if _starts_with(u.text, _words(seg["text"])[0])]
        if right:
            correct.append(min(right) - seg["start"])
        else:
            wrong += 1
        latest: dict[int, str] = {}  # utterance id -> newest text by the deadline
        for t, u in mine:
            if t <= seg["end"] + 0.15:
                latest[u.id] = u.text
        visible = set(_words(" ".join(latest.values())))
        words = _words(seg["text"])
        shown.append(sum(w in visible for w in words) / len(words))
    median = lambda values: round(float(np.median(values)), 2) if values else None
    return {
        "first_text_s": median(first),
        "first_word_s": median(correct),
        "first_wrong": wrong,
        "lines_missed": missed,
        "shown_at_end_pct": round(median(shown) * 100) if shown else None,
    }


def bench(model_arg: str, threads: int, wav: Path, truth: dict, live_arg: str | None = None,
          settings: PipelineSettings | None = None, realtime: bool = False, gain_db: float = 0.0) -> dict:
    model_id, _, device = model_arg.partition(":")
    spec = get_model(model_id)
    options = EngineOptions(num_threads=threads, device=device or "cpu")
    mem0 = gpu_memory_used_mb()
    t = time.perf_counter()
    engine = load_engine(spec, options)
    live = None
    if live_arg:
        live_id, _, live_threads = live_arg.partition(":")
        live = load_streaming_engine(get_model(live_id), int(live_threads or 1))
    load_s = time.perf_counter() - t
    engine.transcribe(np.zeros(16000, np.float32))  # warm-up (CUDA init, kernel caches)
    mem1 = gpu_memory_used_mb()

    data, rate = read_wav(wav)
    audio = data[:, 0] * np.float32(10 ** (gain_db / 20))
    stamped = []  # (seconds into the audio when emitted, utterance)
    me = psutil.Process()
    cpu0, wall0 = sum(me.cpu_times()[:2]), time.perf_counter()
    if realtime:  # emitted when the wall clock says so: decode time counts as delay
        clock = lambda: time.perf_counter() - wall0
    else:  # as fast as possible, timed by the audio fed so far: decode time is free
        clock = lambda: proc.total_samples / rate
    proc = CaptionProcessor(engine, ensure_download(VAD_MODELS["silero"]), settings or PipelineSettings(),
                            lambda u: stamped.append((clock(), u)), start_time=0.0, live_engine=live)
    for i in range(0, len(audio), 320):
        if realtime:  # a block arrives every 20 ms; a slow decode delays the ones queued behind it
            time.sleep(max(0.0, (i + 320) / rate - clock()))
        proc.feed(audio[i : i + 320])
    proc.flush()
    cpu_s, wall_s = sum(me.cpu_times()[:2]) - cpu0, time.perf_counter() - wall0

    events = [u for _, u in stamped]
    finals = [e for e in events if e.final and e.text]
    duration = len(audio) / rate
    hypothesis = " ".join(e.text for e in finals)
    reference = " ".join(s["text"] for s in truth["segments"])
    final_ms = [e.decode_ms for e in finals]
    return {
        "model": model_arg + (f" + live {live_arg}" if live_arg else ""),
        "load_s": round(load_s, 1),
        **live_metrics(stamped, truth),
        "live_ms_p50": round(float(np.median([e.decode_ms for e in events if not e.final] or [0]))),
        "wer": round(word_error_rate(reference, hypothesis) * 100, 1),
        "final_ms_p50": round(float(np.median(final_ms))) if final_ms else None,
        "final_ms_max": round(max(final_ms, default=0)),
        "partials": sum(not e.final for e in events),
        # Fraction of real time spent decoding (partials + finals) = how busy the
        # CPU threads / GPU are on average while people talk this much.
        "decode_duty_pct": round(sum(e.decode_ms for e in events) / 1000 / duration * 100, 1),
        "cpu_cores_avg": round(cpu_s / duration, 2),
        "gpu_mem_mb": None if mem0 is None or mem1 is None else round(mem1 - mem0),
        "wall_s": round(wall_s, 1),
        "text": hypothesis,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("models", nargs="+", help="model ids, optionally :cuda")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--live", action="append", default=[],
                        help="also run each model two-pass with this streaming model id[:threads]; repeatable")
    parser.add_argument("--wav", type=Path, default=ROOT / "tests" / "_out" / "conversation.wav")
    parser.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                        help="override a PipelineSettings field, e.g. --set min_speech=0.1; repeatable")
    parser.add_argument("--realtime", action="store_true",
                        help="feed the audio at real speed, so latencies include decode time (slow)")
    parser.add_argument("--gain-db", type=float, default=0.0, help="scale the audio first, e.g. -30 for quiet")
    parser.add_argument("--json", type=Path, help="also write results here")
    args = parser.parse_args()
    truth = json.loads(args.wav.with_suffix(".json").read_text(encoding="utf-8"))
    settings = PipelineSettings()
    for item in args.set:
        name, _, value = item.partition("=")
        kind = type(getattr(settings, name))
        setattr(settings, name, value.lower() in ("1", "true", "on", "yes") if kind is bool else kind(value))
    results = []
    for m in args.models:
        for live in [None, *args.live]:
            r = bench(m, args.threads, args.wav, truth, live, settings, args.realtime, args.gain_db)
            results.append(r)
            print(f"\n{r['model']}: {r['text']}")
    cols = ["model", "load_s", "wer", "first_text_s", "first_word_s", "first_wrong", "lines_missed",
            "shown_at_end_pct", "live_ms_p50", "final_ms_p50", "partials", "decode_duty_pct", "cpu_cores_avg",
            "gpu_mem_mb"]
    print("\n" + " | ".join(cols))
    for r in results:
        print(" | ".join(str(r[c]) for c in cols))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
