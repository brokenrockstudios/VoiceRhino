"""Command line entry point: GUI by default, plus a few tools for debugging."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from .asr.catalog import DEFAULT_LIVE_MODEL_ID
from .paths import logs_dir


def setup_logging(verbose: bool) -> None:
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(logs_dir() / "voicerhino.log", maxBytes=2_000_000,
                                             backupCount=2, encoding="utf-8")
    ]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def list_devices() -> None:
    from .audio.devices import DEFAULT_OUTPUT_KEY, list_devices as devices

    print(f"  {DEFAULT_OUTPUT_KEY:45}  Default output (follows Windows)")
    for d in devices():
        mark = "*" if d.is_default else " "
        print(f"{mark} {d.key:45}  {d.channels}ch {d.rate} Hz")


def list_models() -> None:
    from .asr.catalog import ASR_MODELS
    from .asr.download import is_downloaded

    for m in ASR_MODELS:
        have = "downloaded" if m.family != "faster_whisper" and is_downloaded(m.source) else f"~{m.size_mb} MB"
        print(f"{m.id:28} {have:>11}  {m.name}")


def run_console(args) -> None:
    """Print captions in the terminal: partials update in place, finals scroll."""
    from .asr.catalog import DEFAULT_MODEL_ID, VAD_MODELS, get_model, supports_live_model
    from .asr.download import ensure_download
    from .asr.engines import EngineOptions, load_engine, load_streaming_engine
    from .audio.file_source import FileSource
    from .audio.wasapi import WasapiSource
    from .audio.devices import DEFAULT_OUTPUT_KEY
    from .pipeline import CaptionPipeline, PipelineSettings
    from .transcript import Utterance

    spec = get_model(args.model or DEFAULT_MODEL_ID)
    print(f"loading {spec.name}…", flush=True)
    progress = lambda d, t: print(f"\rdownloading {d / max(t, 1):.0%}", end="", flush=True)
    engine = load_engine(spec, EngineOptions(num_threads=args.threads, device=args.gpu and "cuda" or "cpu"),
                         progress)
    live = None
    if args.live and supports_live_model(spec):
        live = load_streaming_engine(get_model(args.live), 1, progress)
    source = FileSource(args.file) if args.file else WasapiSource(args.device or DEFAULT_OUTPUT_KEY)
    width = 100

    def show(u: Utterance) -> None:
        if u.final:
            if u.text:
                stamp = time.strftime("%H:%M:%S", time.localtime(u.wall_start))
                print(f"\r{stamp}  {u.text}".ljust(width) + f"  [{u.decode_ms:.0f} ms]", flush=True)
        else:
            print(f"\r… {u.text[-(width - 4):]}".ljust(width), end="", flush=True)

    pipe = CaptionPipeline(source, engine, ensure_download(VAD_MODELS["silero"]), PipelineSettings(), show,
                           live_engine=live)
    pipe.start()
    print(f"listening to {source.label} — Ctrl+C to stop", flush=True)
    try:
        while not (args.file and source.done.is_set()):
            time.sleep(0.2)
        time.sleep(1.5)  # let the last utterance finish
    except KeyboardInterrupt:
        pass
    finally:
        pipe.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voicerhino", description="Live captions for Windows.")
    parser.add_argument("--console", action="store_true", help="print captions in this terminal (no windows)")
    parser.add_argument("--file", type=Path, help="caption a WAV file instead of live audio (demo/testing)")
    parser.add_argument("--device", help="console mode: device key from --list-devices")
    parser.add_argument("--model", help="console mode: model id from --list-models")
    parser.add_argument("--threads", type=int, default=2, help="console mode: CPU threads for recognition")
    parser.add_argument("--live", default=DEFAULT_LIVE_MODEL_ID,
                        help="console mode: streaming model for word-by-word live text, e.g. "
                             "nemotron-streaming-en-160ms-int8 (default: re-decode with the main model)")
    parser.add_argument("--gpu", action="store_true", help="console mode: run the model on the GPU")
    parser.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    parser.add_argument("--list-models", action="store_true", help="show available models and exit")
    parser.add_argument("--reset", action="store_true", help="start with default settings")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(args.verbose)
    if args.list_devices:
        list_devices()
        return 0
    if args.list_models:
        list_models()
        return 0
    if args.console:
        run_console(args)
        return 0
    from .ui.app import run_gui

    return run_gui(reset=args.reset, file=args.file)


if __name__ == "__main__":
    sys.exit(main())
