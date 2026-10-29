"""Try per-app capture (voicerhino.audio.process_loopback) by hand.

    python tools/try_process_loopback.py --list
    python tools/try_process_loopback.py --exe Discord.exe --seconds 5 [--out discord.wav]
    python tools/try_process_loopback.py --pid 1234 --exclude      # everything except 1234

Prints one RMS level per second so you can see whether the app's audio arrives.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # works without install

from voicerhino.audio.process_loopback import (  # noqa: E402
    ProcessLoopbackError,
    ProcessLoopbackSource,
    find_app_pids,
    is_supported,
    list_audio_apps,
)


def print_apps() -> None:
    apps = list_audio_apps()
    if not apps:
        print("No app has an audio session right now.")
    for app in apps:
        print(f"{app.pid:>8}  {'playing' if app.active else 'idle':8} {app.exe}")


def capture(pid: int, args: argparse.Namespace) -> int:
    source = ProcessLoopbackSource(
        pid, exclude=args.exclude, sample_rate=args.rate, channels=args.channels
    )
    lock = threading.Lock()
    window = [0.0, 0]  # sum of squares, sample count since the last report
    kept: list[np.ndarray] = []

    def on_audio(block: np.ndarray, rate: int) -> None:
        with lock:
            window[0] += float(np.square(block, dtype=np.float64).sum())
            window[1] += block.size
            if args.out:
                kept.append(block)

    try:
        source.start(on_audio)
    except ProcessLoopbackError as exc:
        print(f"Could not start: {exc}")
        return 1
    print(
        f"Capturing {source.label}: {source.sample_rate} Hz x{source.channels} "
        f"({source.sample_format}) for {args.seconds:g} s"
    )
    peak = 0.0
    try:
        for second in range(1, int(np.ceil(args.seconds)) + 1):
            time.sleep(1.0)
            with lock:
                (sum_sq, count), window[:] = window, [0.0, 0]
            if not count:
                print(f"{second:4d}s  no audio delivered")
            elif not sum_sq:
                print(f"{second:4d}s  rms 0 (digital silence)")
            else:
                rms = (sum_sq / count) ** 0.5
                peak = max(peak, rms)
                dbfs = 20 * np.log10(rms)
                bar = "#" * int(max(0.0, dbfs + 80) / 2)
                print(f"{second:4d}s  rms {rms:.5f}  {dbfs:6.1f} dBFS  {bar}")
            if not source.running:
                print("Capture ended (target exited or error; see log).")
                break
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
    if source.dropped_frames:
        lost_ms = 1000 * source.dropped_frames / source.sample_rate
        print(f"Windows dropped {lost_ms:.0f} ms because the reader fell behind.")
    print("Audio observed." if peak > 1e-4 else "Only silence observed.")
    if args.out and kept:
        audio = np.concatenate(kept)
        with wave.open(str(args.out), "wb") as wav:
            wav.setnchannels(audio.shape[1])
            wav.setsampwidth(2)
            wav.setframerate(source.sample_rate)
            wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        print(f"Wrote {len(audio) / source.sample_rate:.1f} s to {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--list", action="store_true", help="list apps that own an audio session")
    what.add_argument("--exe", help="capture this executable, e.g. Discord.exe")
    what.add_argument("--pid", type=int, help="capture this process id")
    parser.add_argument("--exclude", action="store_true", help="capture everything except the app")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--out", type=Path, help="also save the capture as a 16-bit WAV")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not is_supported():
        build = sys.getwindowsversion().build
        print(f"Per-app capture needs Windows 11 (build 20348+); this is build {build}.")
        return 1
    if args.list:
        print_apps()
        return 0
    pid = args.pid
    if args.exe:
        pids = find_app_pids(args.exe)
        if not pids:
            print(f"{args.exe} is not running.")
            return 1
        if len(pids) > 1:
            print(f"{args.exe} has {len(pids)} separate instances {pids}; using the oldest.")
        pid = pids[0]
    return capture(pid, args)


if __name__ == "__main__":
    sys.exit(main())
