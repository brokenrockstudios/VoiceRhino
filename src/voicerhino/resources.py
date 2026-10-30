"""How much CPU, memory and GPU VoiceRhino itself uses (Settings › General, history window).

Counts this process and the helpers it starts (llama-server for Hy-MT2), like Task Manager would.
The speech models run inside this process, so their cost is part of these numbers.
GPU use comes from the same Windows performance counters Task Manager shows; on PCs without
them (old Windows, some VMs) the GPU numbers are left out.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    cpu_cores: float  # cores kept busy: 1.0 = one full core
    ram: int  # bytes (working set)
    gpu: float | None = None  # % of the busiest GPU engine; None = can't measure on this PC
    vram: int | None = None  # bytes of dedicated GPU memory


def describe(usage: Usage, cpu_count: int | None = None, long: bool = False) -> str:
    """"CPU 0.5 cores (3%) · RAM 1.2 GB · GPU 0% · VRAM 12 MB"."""
    from .asr.storage import format_size  # here: asr.storage pulls in the model catalogs

    cpus = cpu_count or os.cpu_count() or 1
    share = f"{usage.cpu_cores / cpus:.0%}{' of your CPU' if long else ''}"
    parts = [f"CPU {usage.cpu_cores:.1f} cores ({share})", f"RAM {format_size(usage.ram)}"]
    if usage.vram is not None and usage.vram < 1 << 20 and (usage.gpu or 0) < 0.5:
        parts.append("no GPU use")  # the CPU models: plainer than "GPU 0% · VRAM 0 KB"
        return " · ".join(parts)
    if usage.gpu is not None:
        parts.append(f"GPU {usage.gpu:.0f}%")
    if usage.vram is not None:
        parts.append(f"VRAM {format_size(usage.vram)}")
    return " · ".join(parts)


# --- GPU: Windows performance counters (PDH) --------------------------------------------------

_PDH_FMT_DOUBLE = 0x200
_PDH_FMT_NOCAP100 = 0x8000
_PDH_MORE_DATA = 0x800007D2


class _FmtValue(ctypes.Structure):
    class _Value(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_longlong)]

    _fields_ = [("CStatus", wintypes.DWORD), ("value", _Value)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FmtValue)]


class _GpuCounters:
    """Per-process GPU engine use and dedicated memory, read from "GPU Engine" and
    "GPU Process Memory" counters. Instance names look like "pid_1234_luid_..._engtype_3D"."""

    def __init__(self) -> None:
        self._pdh = ctypes.WinDLL("pdh")
        self._query = wintypes.HANDLE()
        self._check(self._pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)))
        self._engine = self._add(r"\GPU Engine(*)\Utilization Percentage")
        self._memory = self._add(r"\GPU Process Memory(*)\Dedicated Usage")
        self._adapters = self._add(r"\GPU Adapter Memory(*)\Dedicated Usage")
        self._pdh.PdhCollectQueryData(self._query)  # utilization is a rate: needs a first sample

    @staticmethod
    def _check(status: int) -> None:
        if status != 0:
            raise OSError(f"PDH error 0x{status & 0xFFFFFFFF:08X}")

    def _add(self, path: str) -> wintypes.HANDLE:
        counter = wintypes.HANDLE()
        self._check(self._pdh.PdhAddEnglishCounterW(self._query, path, None, ctypes.byref(counter)))
        return counter

    def _values(self, counter: wintypes.HANDLE) -> list[tuple[str, float]]:
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        fmt = _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100
        status = self._pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if status & 0xFFFFFFFF != _PDH_MORE_DATA:
            return []
        buffer = (ctypes.c_byte * size.value)()
        status = self._pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count),
                                                         buffer)
        if status != 0:
            return []
        items = ctypes.cast(buffer, ctypes.POINTER(_FmtItem))
        return [(items[i].szName, items[i].FmtValue.value.doubleValue)
                for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]  # valid / new data

    def sample(self, pids: set[int]) -> tuple[float, int]:
        """(% of the busiest engine type, dedicated bytes) used by these processes."""
        self._pdh.PdhCollectQueryData(self._query)
        prefixes = tuple(f"pid_{pid}_" for pid in pids)
        engines: dict[str, float] = {}
        for name, value in self._values(self._engine):
            if name.startswith(prefixes):
                kind = name.rsplit("engtype_", 1)[-1]
                engines[kind] = engines.get(kind, 0.0) + value
        # Some drivers report nonsense per process (e.g. 150 GB for dwm on a 32 GB card): no process
        # can hold more than its adapter has in use ("pid_1234_luid_..._phys_0" -> "luid_..._phys_0").
        in_use = dict(self._values(self._adapters))
        vram = sum(min(value, in_use.get(name.split("_", 2)[2], value))
                   for name, value in self._values(self._memory) if name.startswith(prefixes))
        return min(100.0, max(engines.values(), default=0.0)), int(vram)

    def close(self) -> None:
        self._pdh.PdhCloseQuery(self._query)


# --- sampling ---------------------------------------------------------------------------------

class ResourceMonitor:
    """Samples this process and its helpers every `interval` seconds on a background thread
    and keeps the last `keep` seconds, for "now" and 1/5-minute averages.

    Helpers come from `helper_pids()` rather than a search for child processes: listing every
    process on the PC holds the GIL for ~25 ms, long enough to make per-app capture drop audio."""

    def __init__(self, interval: float = 2.0, keep: float = 300.0,
                 helper_pids: Callable[[], Iterable[int]] = lambda: ()):
        self.interval = interval
        self._helper_pids = helper_pids
        self._samples: deque[tuple[float, Usage]] = deque(maxlen=max(1, int(keep / interval)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._me = psutil.Process()
        self._procs: dict[int, psutil.Process] = {self._me.pid: self._me}
        self._gpu: _GpuCounters | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="resource-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def latest(self) -> Usage | None:
        with self._lock:
            return self._samples[-1][1] if self._samples else None

    def average(self, seconds: float) -> Usage | None:
        """Mean of the samples in the last `seconds` (None before the first one)."""
        since = time.monotonic() - seconds
        with self._lock:
            recent = [u for t, u in self._samples if t >= since]
        if not recent:
            return None
        gpus = [u.gpu for u in recent if u.gpu is not None]
        vrams = [u.vram for u in recent if u.vram is not None]
        return Usage(sum(u.cpu_cores for u in recent) / len(recent), sum(u.ram for u in recent) // len(recent),
                     sum(gpus) / len(gpus) if gpus else None, sum(vrams) // len(vrams) if vrams else None)

    def _run(self) -> None:
        if sys.platform == "win32":
            try:
                self._gpu = _GpuCounters()
            except Exception as exc:  # no GPU counters on this PC: show CPU and memory only
                log.info("GPU usage unavailable: %s", exc)
        self._me.cpu_percent(None)  # the first reading only starts the measurement
        while not self._stop.wait(self.interval):
            try:
                usage = self.sample()
            except Exception:
                log.exception("resource sample failed")
                continue
            with self._lock:
                self._samples.append((time.monotonic(), usage))
        if self._gpu is not None:
            self._gpu.close()

    def sample(self) -> Usage:
        alive = {self._me.pid} | set(self._helper_pids())
        for pid in alive - self._procs.keys():
            try:
                self._procs[pid] = psutil.Process(pid)
                self._procs[pid].cpu_percent(None)  # starts its measurement; counted from the next sample
            except psutil.Error:
                self._procs.pop(pid, None)
        cpu, ram = 0.0, 0
        for pid in list(self._procs):
            if pid not in alive:
                del self._procs[pid]
                continue
            try:
                with self._procs[pid].oneshot():
                    cpu += self._procs[pid].cpu_percent(None)
                    ram += self._procs[pid].memory_info().rss
            except psutil.Error:
                del self._procs[pid]
        gpu = vram = None
        if self._gpu is not None:
            try:
                gpu, vram = self._gpu.sample(alive)
            except Exception:
                log.exception("GPU sample failed")
        return Usage(cpu / 100, ram, gpu, vram)
