"""Tests for per-app capture (audio/process_loopback.py); skipped where unsupported.

The end-to-end tests play ONE short, quiet tone (1 s at amplitude 0.05) from a child
process and capture it five ways at once. The list_audio_apps test plays a silent file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psutil
import pytest

if sys.platform != "win32":
    pytest.skip("Windows only", allow_module_level=True)

from voicerhino.audio import process_loopback as pl  # noqa: E402

pytestmark = pytest.mark.skipif(
    not pl.is_supported(), reason="process loopback needs Windows build 20348+ (Windows 11)"
)

RATE = 48000
TONE_HZ = 452  # between A4 and A#4, so music playing elsewhere doesn't leak into the checks
TONE_SECONDS = 1.0
TONE_AMPLITUDE = 0.05
# The real interpreter rather than the venv's python.exe redirector, which would add a
# hidden extra process level between the test and the player.
PYTHON = getattr(sys, "_base_executable", "") or sys.executable
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

PLAY = "import sys, winsound; winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME)"
# Waits for a line on stdin, then has a *child* process play the file, so capturing this
# process also checks that the target's children are included.
PLAY_VIA_CHILD = (
    "import subprocess, sys; sys.stdin.readline(); "
    f"subprocess.run([sys.executable, '-c', {PLAY!r}, sys.argv[1]], check=True)"
)
IDLE = "import time; time.sleep(60)"


def _spawn(code: str, *args: object) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-c", code, *map(str, args)], stdin=subprocess.PIPE, text=True
    )


def _kill(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


def _write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((samples * 32767).astype("<i2").tobytes())


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks: list[np.ndarray] = []
        self.rates: set[int] = set()

    def __call__(self, block: np.ndarray, rate: int) -> None:
        with self._lock:
            self._blocks.append(block.copy())
            self.rates.add(rate)

    def audio(self) -> np.ndarray:
        with self._lock:
            return np.concatenate(self._blocks) if self._blocks else np.zeros((0, 1), np.float32)


def peak_rms(audio: np.ndarray, rate: int = RATE) -> float:
    """Loudest 100 ms window."""
    mono = audio.mean(axis=1, dtype=np.float64)
    size = rate // 10
    count = len(mono) // size
    if not count:
        return 0.0
    return float(np.sqrt((mono[: count * size].reshape(count, size) ** 2).mean(axis=1)).max())


def tone_level(audio: np.ndarray, rate: int = RATE) -> float:
    """Amplitude of the test tone: best Hann-windowed 0.5 s lock-in, so other sounds
    (even other tones a few Hz away) barely register."""
    mono = audio.mean(axis=1, dtype=np.float64)
    size = rate // 2
    if len(mono) < size:
        return 0.0
    window = np.hanning(size)
    mixed = mono * np.exp(-2j * np.pi * TONE_HZ * np.arange(len(mono)) / rate)
    starts = range(0, len(mono) - size + 1, rate // 20)
    return float(2 * max(abs(mixed[s : s + size] @ window) for s in starts) / window.sum())


@pytest.fixture(scope="module")
def tone_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    t = np.arange(int(RATE * TONE_SECONDS)) / RATE
    fade = np.minimum(1.0, np.minimum(t, TONE_SECONDS - t) / 0.02)  # no clicks
    path = tmp_path_factory.mktemp("tone") / "tone.wav"
    _write_wav(path, TONE_AMPLITUDE * fade * np.sin(2 * np.pi * TONE_HZ * t))
    return path


@dataclass
class ToneRun:
    include_player: Recorder  # the player process; the tone comes from its child
    include_player_16k: Recorder  # same, asking Windows for 16 kHz mono
    include_player_int16: Recorder  # same, forced onto the 16-bit fallback format
    include_idle: Recorder  # an unrelated, silent process
    exclude_player: Recorder  # everything except the player's tree
    exclude_idle: Recorder  # everything except the idle process, so the tone too
    sample_format: str
    ended_on_exit: bool  # the include capture stopped by itself once the player exited


@pytest.fixture(scope="module")
def tone_run(tone_wav: Path) -> ToneRun:
    player = _spawn(PLAY_VIA_CHILD, tone_wav)
    idle = _spawn(IDLE)
    sources = {
        "include_player": pl.ProcessLoopbackSource(player.pid),
        "include_player_16k": pl.ProcessLoopbackSource(player.pid, sample_rate=16000, channels=1),
        "include_player_int16": pl.ProcessLoopbackSource(player.pid),
        "include_idle": pl.ProcessLoopbackSource(idle.pid),
        "exclude_player": pl.ProcessLoopbackSource(player.pid, exclude=True),
        "exclude_idle": pl.ProcessLoopbackSource(idle.pid, exclude=True),
    }
    recorders = {name: Recorder() for name in sources}
    try:
        for name, source in sources.items():
            formats = pl._FORMATS
            if name == "include_player_int16":  # only offer the fallback format
                pl._FORMATS = tuple(f for f in formats if f.name == "int16")
            try:
                source.start(recorders[name])
            finally:
                pl._FORMATS = formats
        sample_format = sources["include_player"].sample_format
        int16_format = sources["include_player_int16"].sample_format
        time.sleep(0.2)
        player.stdin.write("go\n")
        player.stdin.flush()
        player.wait(30)
        deadline = time.monotonic() + 3
        while sources["include_player"].running and time.monotonic() < deadline:
            time.sleep(0.02)
        ended_on_exit = not sources["include_player"].running
        time.sleep(0.2)
    finally:
        for source in sources.values():
            source.stop()
        _kill(player, idle)
    assert int16_format == "int16"
    return ToneRun(**recorders, sample_format=sample_format, ended_on_exit=ended_on_exit)


def test_include_mode_hears_the_targets_child(tone_run: ToneRun) -> None:
    audio = tone_run.include_player.audio()
    assert tone_run.include_player.rates == {RATE}
    assert tone_run.sample_format == "float32"
    assert audio.dtype == np.float32 and audio.shape[1] == 2
    assert len(audio) >= RATE * TONE_SECONDS
    # 0.05 / sqrt(2) = 0.035 at 100% app volume; the volume mixer can scale it down.
    assert peak_rms(audio) > 1e-3
    assert tone_level(audio) > 2e-3


def test_custom_rate_and_channels(tone_run: ToneRun) -> None:
    audio = tone_run.include_player_16k.audio()
    assert tone_run.include_player_16k.rates == {16000}
    assert audio.dtype == np.float32 and audio.shape[1] == 1
    assert tone_level(audio, 16000) == pytest.approx(
        tone_level(tone_run.include_player.audio()), rel=0.2
    )


def test_int16_fallback_is_scaled_like_float(tone_run: ToneRun) -> None:
    audio = tone_run.include_player_int16.audio()
    assert audio.dtype == np.float32 and audio.shape[1] == 2
    assert tone_level(audio) == pytest.approx(tone_level(tone_run.include_player.audio()), rel=0.2)


def test_include_mode_on_an_idle_process_is_silent(tone_run: ToneRun) -> None:
    audio = tone_run.include_idle.audio()
    # Windows streams zeros continuously, even while the target makes no sound.
    assert len(audio) >= RATE * TONE_SECONDS
    assert peak_rms(audio) < 1e-4


def test_exclude_mode_hears_everything_but_the_target(tone_run: ToneRun) -> None:
    heard = tone_level(tone_run.exclude_idle.audio())
    assert heard > 2e-3
    # Exclude mode also hears every other app on the PC; leave room for their sound.
    assert tone_level(tone_run.exclude_player.audio()) < 0.3 * heard


def test_include_capture_ends_when_the_target_exits(tone_run: ToneRun) -> None:
    assert tone_run.ended_on_exit


def test_start_errors_raise_and_stop_is_idempotent() -> None:
    with pytest.raises(ValueError):
        pl.ProcessLoopbackSource(os.getpid(), include_tree=False)
    gone = subprocess.Popen([PYTHON, "-c", "pass"])
    gone.wait(30)
    source = pl.ProcessLoopbackSource(gone.pid)
    with pytest.raises(pl.ProcessLoopbackError, match="not running"):
        source.start(lambda block, rate: None)
    assert not source.running
    source.stop()
    source.stop()


def test_restart_and_stop_from_inside_the_callback() -> None:
    idle = _spawn(IDLE)
    try:
        source = pl.ProcessLoopbackSource(idle.pid)
        recorder = Recorder()
        source.start(recorder)
        assert source.running
        with pytest.raises(RuntimeError, match="already"):
            source.start(recorder)
        time.sleep(0.2)
        source.stop()
        source.stop()
        assert not source.running
        before = len(recorder.audio())
        assert before > 0

        stopped_inside = threading.Event()

        def stop_on_first_block(block: np.ndarray, rate: int) -> None:
            source.stop()  # must not deadlock by joining its own thread
            stopped_inside.set()

        source.start(stop_on_first_block)
        assert stopped_inside.wait(5)
        deadline = time.monotonic() + 3
        while source.running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not source.running
    finally:
        _kill(idle)


def test_list_audio_apps_runs() -> None:
    apps = pl.list_audio_apps()
    assert isinstance(apps, list)
    for app in apps:
        assert isinstance(app, pl.AudioApp)
        assert app.pid > 0 and app.exe and isinstance(app.active, bool)
    assert [app.exe.casefold() for app in apps] == sorted(app.exe.casefold() for app in apps)
    assert len({app.pid for app in apps}) == len(apps)


@pytest.mark.skipif(not shutil.which("powershell"), reason="needs Windows PowerShell")
def test_list_audio_apps_shows_a_new_player(tmp_path: Path) -> None:
    silence = tmp_path / "silence.wav"  # owns an audio session, but nothing is audible
    _write_wav(silence, np.zeros(RATE))
    quoted = str(silence).replace("'", "''")
    script = f"(New-Object Media.SoundPlayer '{quoted}').PlaySync(); Start-Sleep -Seconds 5"
    player = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    try:
        found: list[pl.AudioApp] = []
        deadline = time.monotonic() + 20
        while not found and time.monotonic() < deadline and player.poll() is None:
            found = [app for app in pl.list_audio_apps() if app.pid == player.pid]
            time.sleep(0.1)
        assert found, "the PowerShell player never showed up in list_audio_apps()"
        assert found[0].exe.casefold() == "powershell.exe"
    finally:
        _kill(player)


def test_find_app_pids_finds_this_python() -> None:
    me = psutil.Process()
    name = me.name()
    root = me
    while (parent := root.parent()) is not None and parent.name().casefold() == name.casefold():
        root = parent
    roots = pl.find_app_pids(name)
    assert root.pid in roots
    assert root.pid in pl.find_app_pids(name.upper())
    assert root.pid in pl.find_app_pids(name.removesuffix(".exe"))
    if me.pid != root.pid:
        assert me.pid not in roots
    for pid in roots:
        try:
            parent = psutil.Process(pid).parent()
        except psutil.NoSuchProcess:
            continue
        assert parent is None or parent.name().casefold() != name.casefold()
    assert pl.find_app_pids("no-such-app-voicerhino-test.exe") == []


def test_app_tree_logic(monkeypatch: pytest.MonkeyPatch) -> None:
    """find_app_pids / list_audio_apps on a made-up process table."""
    table = {
        4: pl._Proc("explorer.exe", 1, 1.0),
        100: pl._Proc("Discord.exe", 4, 10.0),  # main process
        101: pl._Proc("Discord.exe", 100, 11.0),  # renderer
        102: pl._Proc("Discord.exe", 100, 12.0),  # audio service
        200: pl._Proc("python.exe", 4, 20.0),
        201: pl._Proc("python.exe", 200, 21.0),
        202: pl._Proc("python.exe", 201, 22.0),  # grandchild playing audio
        300: pl._Proc("game.exe", 999, 5.0),  # its parent PID 999 was since reused...
        999: pl._Proc("GAME.EXE", 4, 50.0),  # ...by a newer process of the same name
    }
    monkeypatch.setattr(pl, "_process_snapshot", lambda: table)
    assert pl.find_app_pids("discord") == [100]
    assert pl.find_app_pids("PYTHON.EXE") == [200]
    assert pl.find_app_pids("game.exe") == [300, 999]  # oldest first

    sessions = [(101, False), (102, True), (202, True), (300, False), (555, True)]  # 555 exited
    monkeypatch.setattr(pl, "_in_mta_thread", lambda fn: sessions)
    assert pl.list_audio_apps() == [
        pl.AudioApp(100, "Discord.exe", True),
        pl.AudioApp(300, "game.exe", False),
        pl.AudioApp(201, "python.exe", True),  # one level up from 202: Windows can't reach deeper
    ]
