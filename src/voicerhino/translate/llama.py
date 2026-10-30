"""Hy-MT2 through llama.cpp's llama-server, run as a hidden child process.

The server only listens on 127.0.0.1, on a free port, behind a random key, and is tied to
VoiceRhino with a Windows job object so it can't outlive it (even after a crash). Its output is
kept in memory for error messages, never written to disk (it can contain caption text).
"""

from __future__ import annotations

import collections
import ctypes
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

from . import Translator

log = logging.getLogger(__name__)

PROMPT = ("Translate the following text into English. Note that you should only output the "
          "translated result without any additional explanation:\n\n{text}")
THREADS = 4
START_TIMEOUT = 120.0  # s; loading the model from a slow disk can take a while

# No proxy, ever: the requests go to 127.0.0.1 and must never be routed anywhere else.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _JobLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD), ("IoCounters", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_with_us(proc: subprocess.Popen) -> int | None:
    """Put proc in a job that Windows ends when our handle closes, i.e. when we exit.
    Returns the job handle (keep it open), or None where that isn't available."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _JobLimits(LimitFlags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    if not ok or not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
        log.warning("couldn't tie llama-server to VoiceRhino (error %d)", ctypes.get_last_error())
        kernel32.CloseHandle(job)
        return None
    return job


RUNNING: set[int] = set()  # pids of llama-servers we started, counted in our resource use


class HyMTTranslator(Translator):
    def __init__(self, url: str, key: str, proc: subprocess.Popen | None = None, job: int | None = None):
        self.url = url
        self._key = key
        self._proc = proc
        self._job = job
        self._lock = threading.Lock()  # one request at a time (the server has one slot)
        self._close_lock = threading.Lock()  # stop() and the worker thread may both close

    @classmethod
    def start(cls, exe: Path, model: Path, threads: int = THREADS,
              cancel: threading.Event | None = None, timeout: float = START_TIMEOUT) -> "HyMTTranslator":
        port, key = _free_port(), secrets.token_hex(16)
        args = [str(exe), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
                "--api-key", key, "--threads", str(threads), "--ctx-size", "2048", "--parallel", "1",
                "--n-gpu-layers", "0", "--no-webui", "--prio", "-1"]
        flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=flags, cwd=exe.parent)
        job = _kill_with_us(proc)
        RUNNING.add(proc.pid)
        tail: collections.deque[str] = collections.deque(maxlen=15)
        threading.Thread(target=_drain, args=(proc, tail), name="llama-server-output", daemon=True).start()
        self = cls(f"http://127.0.0.1:{port}", key, proc, job)
        deadline = time.monotonic() + timeout
        try:
            while not self._healthy():
                if proc.poll() is not None:
                    raise RuntimeError(f"llama-server exited ({proc.returncode}): " + " | ".join(tail))
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("cancelled")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"llama-server didn't start within {timeout:.0f} s")
                time.sleep(0.25)
        except BaseException:
            self.close()
            raise
        log.info("llama-server ready on port %d (pid %d)", port, proc.pid)
        return self

    def _healthy(self) -> bool:
        try:
            with _OPENER.open(self.url + "/health", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):  # still loading (503) or not listening yet
            return False

    def translate(self, text: str) -> str:
        payload = {
            "messages": [{"role": "user", "content": PROMPT.format(text=text)}],
            # Hy-MT2's recommended sampling, but cooler: captions should read the same each time.
            "temperature": 0.2, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05,
            "max_tokens": min(512, 3 * len(text) + 32),
        }
        request = urllib.request.Request(
            self.url + "/v1/chat/completions", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"})
        with self._lock, _OPENER.open(request, timeout=60) as response:
            reply = json.loads(response.read().decode("utf-8"))
        return reply["choices"][0]["message"]["content"].strip()

    def close(self) -> None:
        with self._close_lock:
            proc, self._proc = self._proc, None
            job, self._job = self._job, None
        if proc is not None:
            RUNNING.discard(proc.pid)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if job:
            ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(job))


def _drain(proc: subprocess.Popen, tail: collections.deque) -> None:
    """Keep the server's last few output lines in memory; a full pipe would stall it."""
    for raw in proc.stdout:
        tail.append(raw.decode("utf-8", "replace").rstrip())
