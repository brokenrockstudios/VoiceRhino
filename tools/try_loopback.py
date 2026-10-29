"""Manual check of live loopback capture: play a WAV quietly on an output device
while captioning that device's loopback through the real pipeline.

    python tools/try_loopback.py tests/_out/conversation.wav --seconds 5 --volume 30
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from voicerhino.asr.catalog import DEFAULT_MODEL_ID, VAD_MODELS, get_model
from voicerhino.asr.download import ensure_download
from voicerhino.asr.engines import EngineOptions, load_engine
from voicerhino.audio.devices import DEFAULT_OUTPUT_KEY
from voicerhino.audio.wasapi import WasapiSource
from voicerhino.pipeline import CaptionPipeline, PipelineSettings
from voicerhino.transcript import TranscriptStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wav", type=Path)
    parser.add_argument("--device", default=DEFAULT_OUTPUT_KEY)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--volume", type=int, default=30, help="ffplay volume 0-100")
    args = parser.parse_args()

    engine = load_engine(get_model(DEFAULT_MODEL_ID), EngineOptions())
    store = TranscriptStore()
    source = WasapiSource(args.device)
    pipe = CaptionPipeline(source, engine, ensure_download(VAD_MODELS["silero"]), PipelineSettings(), store.push)
    pipe.start()
    print(f"capturing {source.label}")
    t0 = time.monotonic()
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-volume", str(args.volume),
                    "-t", str(args.seconds), str(args.wav)], check=True)
    first_partial = None
    while time.monotonic() - t0 < args.seconds + 4:
        if first_partial is None and store.partials():
            first_partial = time.monotonic() - t0
        time.sleep(0.05)
    pipe.stop()
    print(f"peak level seen: {pipe.level:.4f}; first partial after {first_partial}s")
    for u in store.finals():
        print(f"[{u.start - t0:5.2f}-{u.end - t0:5.2f}s] {u.text}")


if __name__ == "__main__":
    main()
