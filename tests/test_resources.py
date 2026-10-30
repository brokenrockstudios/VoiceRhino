"""VoiceRhino's own resource use: sampling, averages and the one-line summary."""

from __future__ import annotations

import subprocess
import sys
import time

from voicerhino.resources import ResourceMonitor, Usage, describe


def test_describe():
    usage = Usage(cpu_cores=0.5, ram=1_288_490_189, gpu=4.2, vram=12 * 1024**2)
    assert describe(usage, cpu_count=16) == "CPU 0.5 cores (3%) · RAM 1.2 GB · GPU 4% · VRAM 12 MB"
    assert describe(usage, cpu_count=16, long=True).startswith("CPU 0.5 cores (3% of your CPU) · ")
    assert describe(Usage(1.0, 50 * 1024**2), cpu_count=4) == "CPU 1.0 cores (25%) · RAM 50 MB"  # no GPU counters
    assert describe(Usage(0.4, 50 * 1024**2, 0.0, 0), cpu_count=4) == "CPU 0.4 cores (10%) · RAM 50 MB · no GPU use"


def test_average_over_recent_samples():
    monitor = ResourceMonitor()
    assert monitor.latest() is None and monitor.average(60) is None
    now = time.monotonic()
    monitor._samples.extend([(now - 200, Usage(4.0, 400, None, None)),  # only in the 5-minute window
                             (now - 30, Usage(1.0, 100, 10.0, 1000)),
                             (now - 10, Usage(0.0, 300, None, 3000))])
    assert monitor.latest() == Usage(0.0, 300, None, 3000)
    assert monitor.average(60) == Usage(0.5, 200, 10.0, 2000)
    assert monitor.average(300).cpu_cores == 5.0 / 3


def test_sample_counts_helper_processes():
    # The base interpreter: a venv's python.exe is a launcher whose child would hold the memory.
    python = getattr(sys, "_base_executable", sys.executable)
    helper = subprocess.Popen([python, "-c", "import time; x = b'x' * 80_000_000; time.sleep(30)"])
    try:
        time.sleep(1.5)  # let it allocate
        alone = ResourceMonitor().sample()
        monitor = ResourceMonitor(helper_pids=lambda: {helper.pid})
        with_helper = monitor.sample()
        assert with_helper.ram - alone.ram > 60_000_000
        helper.kill()
        helper.wait()
        assert monitor.sample().ram < with_helper.ram - 60_000_000  # gone: no longer counted
    finally:
        helper.kill()


def test_monitor_thread_samples_and_stops():
    monitor = ResourceMonitor(interval=0.1)
    monitor.start()
    deadline = time.monotonic() + 5
    while monitor.latest() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    monitor.stop()
    usage = monitor.latest()
    assert usage is not None and usage.ram > 10_000_000 and usage.cpu_cores >= 0
