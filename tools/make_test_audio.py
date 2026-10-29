"""Generate a synthetic multi-speaker conversation with Windows' built-in voices.

Writes <out>.wav (16 kHz mono) and <out>.json (ground truth: speaker, text, start,
end in seconds) so the pipeline, latency and speaker clustering can be tested
without anyone talking.

    python tools/make_test_audio.py tests/_out/conversation
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

RATE = 16000

# (speaker, text). Gaming-voice-chat flavored, with the kind of names and jargon
# that trip recognizers up.
CONVERSATION = [
    ("Mark", "Okay, I'm going to push top lane, can somebody cover the dragon?"),
    ("Zira", "Yeah, I've got it. Give me ten seconds to finish this camp."),
    ("David", "Wait, their jungler just showed up bottom. Be careful."),
    ("Mark", "Did you see that? I almost died to the turret again."),
    ("Zira", "You always say that. Just back off and wait for the team."),
    ("David", "I'm buying the sword now, then I'll head mid."),
    ("Mark", "Alright, everyone group up at the river in thirty seconds."),
    ("Zira", "Can you hear me okay? My microphone was acting weird earlier."),
    ("David", "You sound fine. Let's go, they're pushing the inhibitor."),
]

VOICES = {
    "David": "Microsoft David Desktop",
    "Zira": "Microsoft Zira Desktop",
    "Mark": "Microsoft Mark",
}

_PS_SCRIPT = r"""
param([string]$JobFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$jobs = Get-Content -Raw -Path $JobFile | ConvertFrom-Json
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($job in $jobs) {
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $s.SelectVoice($job.voice)
    $s.Rate = [int]$job.rate
    $s.SetOutputToWaveFile($job.path, $fmt)
    $s.Speak($job.text)
    $s.Dispose()
}
"""


def synthesize(lines: list[tuple[str, str]], workdir: Path, seed: int) -> list[np.ndarray]:
    rng = random.Random(seed)
    jobs = []
    for i, (speaker, text) in enumerate(lines):
        jobs.append(
            {
                "voice": VOICES[speaker],
                "text": text,
                "rate": rng.choice([0, 1, 2]),  # gamers talk fast
                "path": str(workdir / f"line{i:03d}.wav"),
            }
        )
    job_file = workdir / "jobs.json"
    job_file.write_text(json.dumps(jobs), encoding="utf-8")
    script = workdir / "tts.ps1"
    script.write_text(_PS_SCRIPT, encoding="utf-8")
    # PowerShell 7 sees the OneCore voices (e.g. Mark); Windows PowerShell 5.1 doesn't.
    shell = shutil.which("pwsh") or "powershell"
    subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(job_file)],
        check=True,
    )
    clips = []
    for job in jobs:
        with wave.open(job["path"], "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        clips.append(pcm.astype(np.float32) / 32768.0)
    return clips


def build(out: Path, seed: int = 7, lead_in: float = 1.0) -> dict:
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as tmp:
        clips = synthesize(CONVERSATION, Path(tmp), seed)
    pieces = [np.zeros(int(lead_in * RATE), np.float32)]
    t = lead_in
    segments = []
    for (speaker, text), clip in zip(CONVERSATION, clips):
        # Trim the synthesizer's own leading/trailing silence so timings are tight.
        voiced = np.flatnonzero(np.abs(clip) > 0.01)
        clip = clip[voiced[0] : voiced[-1] + 1] if voiced.size else clip
        segments.append({"speaker": speaker, "text": text, "start": round(t, 3), "end": round(t + len(clip) / RATE, 3)})
        gap = rng.uniform(0.7, 1.6)
        pieces += [clip, np.zeros(int(gap * RATE), np.float32)]
        t += len(clip) / RATE + gap
    audio = np.concatenate(pieces)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out.with_suffix(".wav")), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    truth = {"sample_rate": RATE, "duration": round(len(audio) / RATE, 3), "segments": segments}
    out.with_suffix(".json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return truth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="output path without extension")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    truth = build(args.out, args.seed)
    print(f"wrote {args.out.with_suffix('.wav')} ({truth['duration']} s, {len(truth['segments'])} lines)")


if __name__ == "__main__":
    main()
