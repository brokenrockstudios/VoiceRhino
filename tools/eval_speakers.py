"""Compare speaker-embedding models and thresholds for speakers/voice_cluster.py.

Runs each downloaded candidate model (--download fetches them) over synthetic
Windows-TTS speech: tests/_out/conversation (3 voices, regenerated if missing) plus
tests/_out/speakers_long (24 more lines incl. one-word replies, synthesized once).
Conditions: clean, 16k->48k->16k resampling, random per-utterance gain, pink noise and
a game-like tone+noise mix at -20 dB, pink noise at -10 dB. For every threshold the
online clusterer runs in the natural order and in shuffled orders; it prints how often
the speaker<->cluster mapping is perfect, purity, cluster counts and ms per utterance,
then the duration rules, how short clips degrade, and a "similar voices" stress test.

    python tools/eval_speakers.py [--download] [--models titanet-small ...]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
import wave
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import soxr

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from tools.make_test_audio import RATE, build, synthesize  # noqa: E402
from voicerhino.asr.download import ensure_download, is_downloaded  # noqa: E402
from voicerhino.speakers.voice_cluster import (  # noqa: E402
    DEFAULT_SPEAKER_MODEL,
    SPEAKER_MODELS,
    SPEAKER_THRESHOLDS,
    VoiceClusterer,
)

# Evaluated and rejected, kept here so the comparison can be re-run: with sherpa-onnx
# 2.0.1 its embeddings don't even separate a male from a female voice.
REJECTED = {
    "campplus-voxceleb": "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
}
CANDIDATES = {**SPEAKER_MODELS, **REJECTED}

OUT = ROOT / "tests" / "_out"
THRESHOLDS = [round(t, 2) for t in np.arange(0.20, 0.901, 0.05)]
MAIN_CONDITIONS = ["clean", "48k", "gain", "pink-20", "game-20"]  # must all be perfect
SHORT_LENGTHS = [0.3, 0.5, 0.75, 1.0, 1.5, 2.0]
# (min_seconds, new_speaker_seconds) duration rules compared at the tuned threshold.
POLICIES = [(1.0, 0.0), (0.5, 2.0), (0.75, 1.5), (0.75, 2.0), (1.0, 2.0)]

# Extra lines for the same three voices, in a shuffled order with natural repeats.
LONG_LINES = [
    ("Mark", "Nice one."),
    ("Mark", "I need healing over here, I'm almost out."),
    ("Mark", "Their whole team is coming around the left side, fall back now."),
    ("Mark", "Okay."),
    ("Mark", "Who took my loot? That was mine."),
    ("Mark", "Let's try a different strategy this round and stick together."),
    ("Mark", "On my way."),
    ("Mark", "I can't believe that worked, that was amazing."),
    ("Zira", "Behind you!"),
    ("Zira", "I'm reloading, cover me for a second."),
    ("Zira", "There's a sniper on the roof of the red building across the street."),
    ("Zira", "Got him."),
    ("Zira", "My internet is lagging really badly right now, sorry about that."),
    ("Zira", "Wait for me, I'm still respawning."),
    ("Zira", "Yes."),
    ("Zira", "Should we buy armor this round or save money for the next one?"),
    ("David", "Watch out."),
    ("David", "I'm going to flank them from the right while you distract them."),
    ("David", "Does anyone have extra ammo? I'm completely out."),
    ("David", "Good game everyone."),
    ("David", "No."),
    ("David", "The objective is capping, somebody needs to get on the point right now."),
    ("David", "I'll take the boss, you handle the minions."),
    ("David", "That was so close, we almost had them."),
]


# -- data --------------------------------------------------------------------------


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0


def load_set(stem: Path) -> list[tuple[str, np.ndarray]]:
    truth = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
    audio, sr = read_wav(stem.with_suffix(".wav")), truth["sample_rate"]
    return [(s["speaker"], audio[round(s["start"] * sr) : round(s["end"] * sr)]) for s in truth["segments"]]


def ensure_conversation() -> Path:
    stem = OUT / "conversation"
    if not (stem.with_suffix(".wav").exists() and stem.with_suffix(".json").exists()):
        build(stem)
    return stem


def ensure_long_set(seed: int = 11) -> Path:
    stem = OUT / "speakers_long"
    if stem.with_suffix(".wav").exists() and stem.with_suffix(".json").exists():
        return stem
    rng = random.Random(seed)
    lines = LONG_LINES[:]
    rng.shuffle(lines)
    with tempfile.TemporaryDirectory() as tmp:
        clips = synthesize(lines, Path(tmp), seed)
    pieces, segments, t = [np.zeros(RATE, np.float32)], [], 1.0
    for (speaker, text), clip in zip(lines, clips):
        voiced = np.flatnonzero(np.abs(clip) > 0.01)  # trim like make_test_audio.build
        clip = clip[voiced[0] : voiced[-1] + 1] if voiced.size else clip
        segments.append({"speaker": speaker, "text": text, "start": round(t, 3), "end": round(t + len(clip) / RATE, 3)})
        gap = rng.uniform(0.7, 1.6)
        pieces += [clip, np.zeros(int(gap * RATE), np.float32)]
        t += len(clip) / RATE + gap
    audio = np.concatenate(pieces)
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(stem.with_suffix(".wav")), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    truth = {"sample_rate": RATE, "duration": round(len(audio) / RATE, 3), "segments": segments}
    stem.with_suffix(".json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return stem


# -- degradations ------------------------------------------------------------------


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) or 1e-9


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(spectrum.size, dtype=np.float64)
    f[0] = 1.0
    x = np.fft.irfft(spectrum / np.sqrt(f), n)
    return (x / rms(x)).astype(np.float32)


def game_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """Engine hum + a pulsing chord + pink noise: a crude in-game soundtrack."""
    t = np.arange(n) / RATE
    partials = [(55, 1.0), (110, 0.7), (220, 0.4), (523.3, 0.5), (659.3, 0.4), (784.0, 0.3)]
    tones = sum(a * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi)) for f, a in partials)
    tones = tones * (0.6 + 0.4 * np.sin(2 * np.pi * rng.uniform(0.3, 2.0) * t))
    x = tones / rms(tones) + 0.7 * pink_noise(n, rng)
    return (x / rms(x)).astype(np.float32)


def mix(clip: np.ndarray, noise: np.ndarray, db: float) -> np.ndarray:
    return (clip + noise * rms(clip) * 10 ** (db / 20)).astype(np.float32)


def roundtrip_48k(clip: np.ndarray) -> np.ndarray:
    return soxr.resample(soxr.resample(clip, RATE, 48000), 48000, RATE).astype(np.float32)


CONDITIONS = {
    "clean": lambda x, rng: x,
    "48k": lambda x, rng: roundtrip_48k(x),
    "gain": lambda x, rng: np.clip(x * 10 ** (rng.uniform(-20, 6) / 20), -1, 1).astype(np.float32),
    "pink-20": lambda x, rng: mix(x, pink_noise(len(x), rng), -20),
    "game-20": lambda x, rng: mix(x, game_noise(len(x), rng), -20),
    "pink-10": lambda x, rng: mix(x, pink_noise(len(x), rng), -10),
}


def shift_voice(clip: np.ndarray, factor: float) -> np.ndarray:
    """Tape-speed change: pitch and formants up by `factor` (a 'similar' new voice)."""
    return soxr.resample(clip, RATE, RATE / factor).astype(np.float32)


# -- scoring -----------------------------------------------------------------------


def run_online(vc: VoiceClusterer, embs, durs, order, threshold, min_s, new_s) -> list[int | None]:
    """The online clusterer over precomputed embeddings, with given duration rules."""
    vc.threshold, vc.new_speaker_seconds = threshold, new_s
    vc.reset()
    out = []
    for i in order:
        if embs[i] is None or durs[i] < min_s:
            out.append(None)  # what embed() returns under min_seconds
        else:
            out.append(vc.assign_embedding(embs[i], durs[i]))
    return out


def score(labels: list[str], clusters: list[int | None]) -> tuple[float, float, int, bool, float]:
    """(purity, inverse purity, #clusters, perfect 1:1 mapping, assigned fraction)."""
    by_cluster: dict[int, Counter] = defaultdict(Counter)
    by_speaker: dict[str, Counter] = defaultdict(Counter)
    for label, cluster in zip(labels, clusters):
        if cluster is not None:
            by_cluster[cluster][label] += 1
            by_speaker[label][cluster] += 1
    n = sum(sum(c.values()) for c in by_cluster.values()) or 1
    purity = sum(max(c.values()) for c in by_cluster.values()) / n
    inverse = sum(max(c.values()) for c in by_speaker.values()) / n
    perfect = purity == 1.0 and inverse == 1.0 and len(by_speaker) == len(set(labels))
    return purity, inverse, len(by_cluster), perfect, n / len(labels)


def sweep(vc, embs, durs, labels, orders, thresholds, min_s, new_s) -> dict:
    """threshold -> (perfect %, purity, inverse purity, #clusters, assigned %), means over orders."""
    result = {}
    for thr in thresholds:
        rows = [
            score([labels[i] for i in order], run_online(vc, embs, durs, order, thr, min_s, new_s))
            for order in orders
        ]
        p, ip, k, ok, cov = zip(*rows)
        result[thr] = (100.0 * np.mean(ok), float(np.mean(p)), float(np.mean(ip)), float(np.mean(k)), 100.0 * np.mean(cov))
    return result


def pair_stats(embs: list, labels: list[str]) -> tuple[float, float, float, float]:
    """(same-speaker min, same mean, cross-speaker max, cross mean) pairwise cosine."""
    idx = [i for i, e in enumerate(embs) if e is not None]
    same, cross = [], []
    for a in range(len(idx)):
        for b in range(a + 1, len(idx)):
            i, j = idx[a], idx[b]
            (same if labels[i] == labels[j] else cross).append(float(embs[i] @ embs[j]))
    return min(same), float(np.mean(same)), max(cross), float(np.mean(cross))


def table(headers: list[str], rows: list[list]) -> str:
    cells = [headers] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.rjust(w) for c, w in zip(r, widths)) for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def ranges(values: list[float]) -> str:
    """[0.3, 0.35, 0.4, 0.6] -> '0.30-0.40, 0.60'."""
    if not values:
        return "none"
    out, start, prev = [], values[0], values[0]
    for v in values[1:] + [None]:
        if v is not None and abs(v - prev - 0.05) < 1e-6:
            prev = v
            continue
        out.append(f"{start:.2f}" if start == prev else f"{start:.2f}-{prev:.2f}")
        if v is not None:
            start = prev = v
    return ", ".join(out)


# -- evaluation --------------------------------------------------------------------


def evaluate(name: str, path: Path, utts: list[tuple[str, np.ndarray]], args) -> dict:
    print(f"\n=== {name}  ({path.name}, {path.stat().st_size / 1e6:.0f} MB)")
    vc = VoiceClusterer(path, num_threads=args.threads)
    min_s, new_s = vc.min_seconds, vc.new_speaker_seconds
    vc.min_seconds = 0.0  # embed everything once; run_online applies the duration rules
    labels = [s for s, _ in utts]
    durs = [len(c) / RATE for _, c in utts]
    shuffler = random.Random(1)
    orders = [list(range(len(utts)))]
    for _ in range(args.shuffles):
        orders.append(shuffler.sample(orders[0], len(utts)))

    embs_by_cond, sweeps, stats, ms_clean = {}, {}, {}, []
    for cond, degrade in CONDITIONS.items():
        rng = np.random.default_rng(5)
        embs = []
        for _, clip in utts:
            x = degrade(clip, rng)
            t0 = time.perf_counter()
            embs.append(vc.embed(x))
            if cond == "clean":
                ms_clean.append((time.perf_counter() - t0) * 1000)
        embs_by_cond[cond] = embs
        sweeps[cond] = sweep(vc, embs, durs, labels, orders, THRESHOLDS, min_s, new_s)
        stats[cond] = pair_stats([e if d >= 1.0 else None for e, d in zip(embs, durs)], labels)
    tuned = SPEAKER_THRESHOLDS.get(name)
    best = max(THRESHOLDS, key=lambda t: sum(sweeps[c][t][0] for c in CONDITIONS))
    thr = tuned if tuned is not None else best
    print(
        f"{len(utts)} utterances, {len(set(labels))} voices, {len(orders)} orders (natural + {args.shuffles} "
        f"shuffled); rules: min_seconds={min_s}, new_speaker_seconds={new_s}"
    )
    print("pairwise cosine of utterances >= 1 s: same-speaker min/mean | cross-speaker max/mean")
    for cond, (smin, smean, cmax, cmean) in stats.items():
        print(f"  {cond:8s} {smin:5.2f} / {smean:4.2f} | {cmax:5.2f} / {cmean:4.2f}   margin {smin - cmax:+.2f}")
    print("perfect-mapping % of orders (mean #clusters) per threshold:")
    rows = [
        [f"{t:.2f}"] + [f"{sweeps[c][t][0]:3.0f}% ({sweeps[c][t][3]:.1f})" for c in CONDITIONS] for t in THRESHOLDS
    ]
    print(table(["thr"] + list(CONDITIONS), rows))
    good = [t for t in THRESHOLDS if all(sweeps[c][t][0] == 100.0 for c in MAIN_CONDITIONS)]
    print(f"thresholds perfect in every order for {', '.join(MAIN_CONDITIONS)}: {ranges(good)}")
    print(f"threshold used below: {thr:.2f} ({'tuned' if tuned is not None else 'best in sweep'})")
    at = {c: sweeps[c][thr] for c in CONDITIONS}
    print("  " + ", ".join(f"{c} {v[0]:.0f}% P={v[1]:.3f} IP={v[2]:.3f}" for c, v in at.items()))

    print("duration rules at that threshold, perfect % / assigned % of utterances:")
    rows = []
    for p_min, p_new in POLICIES:
        cells = []
        for cond in CONDITIONS:
            r = sweep(vc, embs_by_cond[cond], durs, labels, orders, [thr], p_min, p_new)[thr]
            cells.append(f"{r[0]:3.0f}% / {r[4]:3.0f}%")
        rule = f"min {p_min:.2f} s, new >= {p_new:.1f} s" + (" (default)" if (p_min, p_new) == (min_s, new_s) else "")
        rows.append([rule] + cells)
    print(table(["rule"] + list(CONDITIONS), rows))

    long_clip = np.concatenate([clip for _, clip in utts])[: 15 * RATE]
    t0 = time.perf_counter()
    for _ in range(3):
        vc.embed(long_clip)  # a 15 s utterance is cut to vc.max_seconds
    ms_15s = (time.perf_counter() - t0) * 1000 / 3

    short = short_clips(vc, utts, embs_by_cond["clean"], durs, thr)
    similar = similar_voices(vc, utts, thr, min_s, args)
    return {
        "dim": vc.dim,
        "ms_mean": float(np.mean(ms_clean)),
        "ms_max": float(np.max(ms_clean)),
        "ms_15s": ms_15s,
        "margin_clean": stats["clean"][0] - stats["clean"][2],
        "margin_pink20": stats["pink-20"][0] - stats["pink-20"][2],
        "good": good,
        "thr": thr,
        "at": at,
        "short": short,
        "similar": similar,
    }


def short_clips(vc: VoiceClusterer, utts, full_embs: list, durs: list[float], thr: float) -> dict:
    """Crop utterances to L seconds and score them against leave-one-out centroids
    (mean of the speaker's other full-length clean utterances)."""
    labels = [s for s, _ in utts]
    speakers = sorted(set(labels))
    rng = np.random.default_rng(3)
    results, rows = {}, []
    for cond in ["clean", "pink-20", "game-20", "pink-10"]:
        for length in SHORT_LENGTHS:
            n = right = join_right = below = join_wrong = foreign = 0
            for i, (speaker, clip) in enumerate(utts):
                if durs[i] < length + 0.2 or full_embs[i] is None:
                    continue
                refs = []
                for s in speakers:
                    members = [e for j, e in enumerate(full_embs) if labels[j] == s and j != i and durs[j] >= 1.0]
                    c = np.mean(members, axis=0)
                    refs.append(c / np.linalg.norm(c))
                refs = np.stack(refs)
                own = speakers.index(speaker)
                for _ in range(3):
                    start = int(rng.integers(0, len(clip) - int(length * RATE)))
                    e = vc.embed(CONDITIONS[cond](clip[start : start + int(length * RATE)], rng))
                    if e is None:  # all-silent crop (pause between sentences)
                        continue
                    sims = refs @ e
                    top = int(np.argmax(sims))
                    n += 1
                    right += top == own
                    if sims[top] < thr:
                        below += 1
                    elif top == own:
                        join_right += 1
                    else:
                        join_wrong += 1
                    foreign += float(np.delete(sims, own).max()) >= thr
            pct = lambda k: 100.0 * k / max(n, 1)  # noqa: E731
            results[(cond, length)] = (n, pct(right), pct(join_right), pct(below), pct(join_wrong), pct(foreign))
            rows.append(
                [cond, f"{length:.2f}s", n] + [f"{pct(k):.0f}%" for k in (right, join_right, below, join_wrong, foreign)]
            )
    print(f"short clips vs leave-one-out speaker centroids (threshold {thr:.2f}, 3 random crops per utterance):")
    print("  'below thr' = None if shorter than new_speaker_seconds, else a junk cluster; 'foreign >= thr' =")
    print("  a not-yet-clustered speaker's clip would wrongly join someone else.")
    print(table(["cond", "len", "n", "nearest ok", "joins right", "below thr", "joins wrong", "foreign >= thr"], rows))
    return results


def similar_voices(vc: VoiceClusterer, utts, thr: float, min_s: float, args) -> dict:
    """6 voices: the 3 TTS voices + the same voices pitch/formant-shifted up 12%."""
    shifted = [(s + "^", shift_voice(clip, 1.12)) for s, clip in utts]
    both = utts + shifted
    labels = [s for s, _ in both]
    durs = [len(c) / RATE for _, c in both]
    embs = [vc.embed(clip) for _, clip in both]
    copy_sims = defaultdict(list)
    for i, (s, _) in enumerate(utts):
        a, b = embs[i], embs[len(utts) + i]
        if a is not None and b is not None and durs[i] >= 1.0:
            copy_sims[s].append(float(a @ b))
    shuffler = random.Random(2)
    orders = [shuffler.sample(range(len(both)), len(both)) for _ in range(args.shuffles + 1)]
    result = sweep(vc, embs, durs, labels, orders, THRESHOLDS, min_s, vc.new_speaker_seconds)
    print("similar voices (3 voices + their 12% pitch-shifted copies = 6), shuffled orders, perfect % (#clusters):")
    print("  voice vs its own shifted copy, mean cosine: " + ", ".join(f"{s} {np.mean(v):.2f}" for s, v in copy_sims.items()))
    print("  " + "  ".join(f"{t:.2f}:{result[t][0]:.0f}%({result[t][3]:.1f})" for t in THRESHOLDS))
    return {"at": result[thr] if thr in result else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="*", default=list(CANDIDATES), help=", ".join(CANDIDATES))
    parser.add_argument("--download", action="store_true", help="download missing candidate models first")
    parser.add_argument("--shuffles", type=int, default=49, help="extra random utterance orders")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--no-long", action="store_true", help="only the 9-line conversation")
    args = parser.parse_args()

    utts = load_set(ensure_conversation())
    if not args.no_long:
        try:
            utts += load_set(ensure_long_set())
        except Exception as exc:  # no PowerShell / TTS voices: keep going with the base set
            print(f"(long set unavailable: {exc!r})")
    durations = sorted(len(c) / RATE for _, c in utts)
    print(
        f"{len(utts)} utterances, {durations[0]:.2f}-{durations[-1]:.2f} s "
        f"({sum(d < 1 for d in durations)} under 1 s), voices {sorted({s for s, _ in utts})}"
    )

    summary = {}
    for name in args.models:
        url = CANDIDATES[name]
        if not (args.download or is_downloaded(url)):
            print(f"\n=== {name}: not downloaded, skipped (use --download)")
            continue
        summary[name] = evaluate(name, ensure_download(url), utts, args)

    print("\n=== summary (1 CPU thread; 'perfect' = 1:1 speaker<->cluster mapping in every order)")
    rows = []
    for name, r in summary.items():
        short = r["short"]
        rows.append(
            [
                name + (" (default)" if name == DEFAULT_SPEAKER_MODEL else ""),
                r["dim"],
                f"{r['ms_mean']:.0f} / {r['ms_max']:.0f}",
                f"{r['ms_15s']:.0f}",
                f"{r['margin_clean']:+.2f} / {r['margin_pink20']:+.2f}",
                ranges(r["good"]),
                f"{r['thr']:.2f}",
                " ".join(f"{r['at'][c][0]:.0f}" for c in CONDITIONS),
                f"{short[('clean', 1.0)][1]:.0f} / {short[('pink-20', 1.0)][1]:.0f}",
                f"{r['similar']['at'][0]:.0f}%" if r["similar"]["at"] else "-",
            ]
        )
    print(
        table(
            ["model", "dim", "ms/utt mean/max", "ms 15s utt", "margin clean/pink-20", "perfect thr range", "thr",
             "% perfect @thr " + "/".join(CONDITIONS), "1 s clip nearest ok % clean/pink-20", "6 similar voices"],
            rows,
        )
    )


if __name__ == "__main__":
    main()
