"""Per-app audio capture via Windows process loopback (build 20348+, i.e. Windows 11).

ProcessLoopbackSource hears only one app (e.g. Discord and its helper processes), or
everything except it, without a virtual cable. list_audio_apps() feeds a "capture this
app" picker; find_app_pids("Discord.exe") resolves a saved exe name to a PID.

Tested on Win11 build 26200 by tests/test_process_loopback.py (a child process plays a
quiet tone: its tree hears it, an idle process hears exact silence, exclude mode
inverts both) and by hand with tools/try_process_loopback.py against Discord.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import traceback
from ctypes import POINTER, Structure, byref, c_int, c_long, c_longlong, c_uint32, c_uint64
from ctypes import c_ulong, c_ushort, c_void_p, sizeof, wintypes
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import comtypes
import numpy as np
import psutil
from comtypes import COMMETHOD, GUID, HRESULT, COMError, COMObject, IUnknown

from .base import AudioCallback, AudioSource

__all__ = [
    "MIN_WINDOWS_BUILD",
    "AudioApp",
    "ProcessLoopbackError",
    "ProcessLoopbackSource",
    "find_app_pids",
    "is_supported",
    "list_audio_apps",
]

log = logging.getLogger(__name__)

MIN_WINDOWS_BUILD = 20348  # Windows Server 2022 / Windows 11; 21H2 Windows 10 lacks it

_VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_MODE_INCLUDE_TREE = 0
_MODE_EXCLUDE_TREE = 1
_VT_BLOB = 65

_SHAREMODE_SHARED = 0
_STREAMFLAGS = (
    0x00020000  # AUDCLNT_STREAMFLAGS_LOOPBACK
    | 0x00040000  # AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    | 0x80000000  # AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
    | 0x08000000  # AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
)
_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_IEEE_FLOAT = 3

# Requested buffer length. Process-loopback clients ignore it (build 26200): Windows uses a
# 10 ms buffer, queues at most ~3 packets and silently drops older ones, so the capture
# thread runs at MMCSS "Audio" priority and counts drops via packet timestamps.
_BUFFER_HNS = 10_000_000
_DROP_LOG_INTERVAL_S = 30.0
_ACTIVATE_TIMEOUT_S = 5.0
_START_TIMEOUT_S = 20.0
_STOP_TIMEOUT_S = 5.0
_ENUMERATE_TIMEOUT_S = 10.0
_POLL_MS = 100  # safety net in case the sample-ready event ever stays quiet

_E_RENDER = 0
_DEVICE_STATE_ACTIVE = 0x1
_SESSION_STATE_ACTIVE = 1
_CLSCTX_ALL = 23
_COINIT_MULTITHREADED = 0
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_FAILED = 0xFFFFFFFF

_HRESULT_NAMES = {
    0x80004001: "E_NOTIMPL",
    0x80004002: "E_NOINTERFACE",
    0x80004003: "E_POINTER",
    0x80004005: "E_FAIL",
    0x8000000E: "E_ILLEGAL_METHOD_CALL",
    0x80070005: "E_ACCESSDENIED",
    0x8007000E: "E_OUTOFMEMORY",
    0x80070057: "E_INVALIDARG",
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED",
    0x88890003: "AUDCLNT_E_WRONG_ENDPOINT_TYPE",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    0x88890010: "AUDCLNT_E_SERVICE_NOT_RUNNING",
    0x88890011: "AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED",
    0x88890014: "AUDCLNT_E_EVENTHANDLE_NOT_SET",
    0x88890021: "AUDCLNT_E_INVALID_STREAM_FLAG",
    0x88890026: "AUDCLNT_E_RESOURCES_INVALIDATED",
}


# --- Win32 plumbing (private DLL handles so we never clobber other modules' argtypes) ---

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD

_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE_VALUE = c_void_p(-1).value


class _ProcessEntry32W(Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, POINTER(_ProcessEntry32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, POINTER(_ProcessEntry32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL

_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None

_avrt = ctypes.WinDLL("avrt")
_avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, POINTER(wintypes.DWORD)]
_avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
_avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
_avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL


def _hr_text(hr: int) -> str:
    code = hr & 0xFFFFFFFF
    name = _HRESULT_NAMES.get(code) or ctypes.FormatError(hr).strip()
    return f"0x{code:08X}" if name == "<no description>" else f"0x{code:08X} {name}"


class ProcessLoopbackError(RuntimeError):
    """Capture could not start. `hresult` holds the failing Windows error code, if any."""

    def __init__(self, message: str, hresult: int | None = None) -> None:
        super().__init__(message)
        self.hresult = hresult


_T = TypeVar("_T")


def _call(what: str, fn: Callable[..., _T], *args: Any) -> _T:
    try:
        return fn(*args)
    except COMError as exc:
        raise ProcessLoopbackError(f"{what} failed: {_hr_text(exc.hresult)}", exc.hresult) from None


def _drop_frame_locals(exc: BaseException | None) -> None:
    """Free the locals a stored exception's traceback pins (COM pointers among them), so they
    are released on the thread that owns them rather than whenever the exception dies."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        traceback.clear_frames(exc.__traceback__)
        exc = exc.__cause__ or exc.__context__


def _slots(*names: str) -> list:
    """Placeholder vtable entries for methods we never call (only their position matters)."""
    return [COMMETHOD([], HRESULT, name) for name in names]


# --- Structures (audioclientactivationparams.h, propidl.h, mmreg.h) ---


class _ProcessLoopbackParams(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


class _ActivationParams(Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", _ProcessLoopbackParams)]


class _Blob(Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class _PropVariantBlob(Structure):
    """A PROPVARIANT holding VT_BLOB; same size and layout as the real union."""

    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("blob", _Blob),
    ]


class _WaveFormatEx(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


# --- COM interfaces (audioclient.h, mmdeviceapi.h, audiopolicy.h) ---


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetActivateResult",
            (["out"], POINTER(c_long), "activateResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        # Typed as void* so comtypes never wraps (and later over-releases) the pointer.
        COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], c_void_p, "activateOperation")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Initialize",
            (["in"], c_int, "ShareMode"),
            (["in"], wintypes.DWORD, "StreamFlags"),
            (["in"], c_longlong, "hnsBufferDuration"),
            (["in"], c_longlong, "hnsPeriodicity"),
            (["in"], POINTER(_WaveFormatEx), "pFormat"),
            (["in"], POINTER(GUID), "AudioSessionGuid"),
        ),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        *_slots("GetStreamLatency", "GetCurrentPadding", "IsFormatSupported", "GetMixFormat"),
        *_slots("GetDevicePeriod"),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD(
            [],
            HRESULT,
            "GetService",
            (["in"], POINTER(GUID), "riid"),
            (["out"], POINTER(POINTER(IUnknown)), "ppv"),
        ),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(c_void_p), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD(
            [], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")
        ),
    ]


class IAudioSessionControl2(IUnknown):
    _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
        *_slots("GetDisplayName", "SetDisplayName", "GetIconPath", "SetIconPath"),
        *_slots("GetGroupingParam", "SetGroupingParam"),
        *_slots("RegisterAudioSessionNotification", "UnregisterAudioSessionNotification"),
        *_slots("GetSessionIdentifier", "GetSessionInstanceIdentifier"),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "pRetVal")),
        COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
    ]


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
        COMMETHOD(
            [],
            HRESULT,
            "GetSession",
            (["in"], c_int, "SessionCount"),
            (["out"], POINTER(POINTER(IUnknown)), "Session"),
        ),
    ]


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = [
        *_slots("GetAudioSessionControl", "GetSimpleAudioVolume"),
        COMMETHOD(
            [],
            HRESULT,
            "GetSessionEnumerator",
            (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum"),
        ),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Activate",
            (["in"], POINTER(GUID), "iid"),
            (["in"], wintypes.DWORD, "dwClsCtx"),
            (["in"], c_void_p, "pActivationParams"),
            (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
        ),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint32), "pcDevices")),
        COMMETHOD(
            [],
            HRESULT,
            "Item",
            (["in"], c_uint32, "nDevice"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppDevice"),
        ),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "EnumAudioEndpoints",
            (["in"], c_int, "dataFlow"),
            (["in"], wintypes.DWORD, "dwStateMask"),
            (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices"),
        ),
    ]


_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

_mmdevapi = ctypes.WinDLL("mmdevapi")
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.argtypes = [
    wintypes.LPCWSTR,
    POINTER(GUID),
    POINTER(_PropVariantBlob),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
]
_ActivateAudioInterfaceAsync.restype = c_long


class _ActivationHandler(COMObject):
    """Completion callback; Windows calls it on one of its own MTA threads, hence IAgileObject."""

    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()

    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, this, operation) -> int:
        self.done.set()  # the result is read on the capture thread via our own operation ref
        return 0


# --- Public helpers ---


def is_supported() -> bool:
    """True when this Windows build has process loopback (build 20348+: Windows 11)."""
    return sys.platform == "win32" and sys.getwindowsversion().build >= MIN_WINDOWS_BUILD


@dataclass(frozen=True)
class AudioApp:
    pid: int  # pass to ProcessLoopbackSource; the app root for normal apps
    exe: str  # e.g. "Discord.exe"
    active: bool  # some session of the app is playing right now


@dataclass
class _Proc:
    name: str
    ppid: int
    created: float | None = None  # filled lazily: only same-exe parent/child pairs need it

    def create_time(self, pid: int) -> float:
        if self.created is None:
            try:
                self.created = psutil.Process(pid).create_time()
            except (psutil.Error, ValueError):
                self.created = 0.0
        return self.created


def _norm(exe: str) -> str:
    exe = exe.strip().casefold()
    return exe[:-4] if exe.endswith(".exe") else exe


def _process_snapshot() -> dict[int, _Proc]:
    """pid -> (exe name, parent pid) for every process, from one ToolHelp snapshot.

    psutil's ppid() takes a whole-system snapshot per call on Windows (~30 s for 800
    processes when its cache is cold), so the tree comes from here and psutil only
    supplies create times."""
    handle = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    snapshot = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = sizeof(entry)
        ok = _kernel32.Process32FirstW(handle, byref(entry))
        while ok:
            snapshot[entry.th32ProcessID] = _Proc(entry.szExeFile, entry.th32ParentProcessID)
            ok = _kernel32.Process32NextW(handle, byref(entry))
    finally:
        _kernel32.CloseHandle(handle)
    return snapshot


def _same_app_parent(pid: int, snapshot: dict[int, _Proc]) -> int | None:
    """The parent PID if it is the same exe (and not a reused PID), else None."""
    me = snapshot[pid]
    parent = snapshot.get(me.ppid)
    if parent is None or me.ppid == pid or _norm(parent.name) != _norm(me.name):
        return None
    born, parent_born = me.create_time(pid), parent.create_time(me.ppid)
    if born and parent_born and parent_born > born:
        return None  # the real parent died and its PID was reused by a newer process
    return me.ppid


def find_app_pids(exe_name: str) -> list[int]:
    """Root PIDs of every running instance of exe_name ("Discord.exe"; case-insensitive,
    ".exe" optional), oldest first. A root is a process whose parent is not the same exe;
    capturing it also captures its child processes, which for Discord and other
    Chromium/Electron apps is every helper process (they are all direct children)."""
    target = _norm(exe_name)
    snapshot = _process_snapshot()
    roots = [
        pid
        for pid, proc in snapshot.items()
        if pid and _norm(proc.name) == target and _same_app_parent(pid, snapshot) is None
    ]
    return sorted(roots, key=lambda pid: (snapshot[pid].create_time(pid), pid))


def list_audio_apps() -> list[AudioApp]:
    """Apps that own an audio session on an active output device, sorted by exe name.

    Each session is mapped to the PID to capture: its process's parent when that is the
    same exe, else the process itself. For normal apps (Discord: main process -> renderer
    and audio-service children) that is the app root, as find_app_pids returns, and one
    entry per app. It stops one level up rather than at the root because Windows only
    captures the target's direct children (see ProcessLoopbackSource)."""
    sessions = _in_mta_thread(_render_session_pids)
    snapshot = _process_snapshot()
    apps: dict[int, AudioApp] = {}
    for pid, active in sessions:
        if pid not in snapshot:
            continue  # stale session of a process that already exited
        target = _same_app_parent(pid, snapshot) or pid
        seen_active = target in apps and apps[target].active
        apps[target] = AudioApp(target, snapshot[target].name, active or seen_active)
    return sorted(apps.values(), key=lambda app: (app.exe.casefold(), app.pid))


def _in_mta_thread(fn: Callable[[], _T]) -> _T:
    """Run fn on a short-lived MTA thread so the caller's COM apartment never matters."""
    box: dict[str, Any] = {}

    def run() -> None:
        hr = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            _drop_frame_locals(exc)
            box["error"] = exc
        finally:
            if hr >= 0:
                _ole32.CoUninitialize()

    thread = threading.Thread(target=run, name="voicerhino-audio-sessions", daemon=True)
    thread.start()
    thread.join(_ENUMERATE_TIMEOUT_S)
    if thread.is_alive():
        raise TimeoutError("enumerating audio sessions timed out")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _render_session_pids() -> list[tuple[int, bool]]:
    """(pid, is_active) of every non-system audio session on every active render endpoint."""
    found = []
    enumerator = comtypes.CoCreateInstance(
        _CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, _CLSCTX_ALL
    )
    devices = enumerator.EnumAudioEndpoints(_E_RENDER, _DEVICE_STATE_ACTIVE)
    for index in range(devices.GetCount()):
        try:
            device = devices.Item(index)
            manager = device.Activate(
                byref(IAudioSessionManager2._iid_), _CLSCTX_ALL, None
            ).QueryInterface(IAudioSessionManager2)
            sessions = manager.GetSessionEnumerator()
            count = sessions.GetCount()
        except COMError as exc:  # device unplugged mid-enumeration, etc.
            log.debug("skipping render device %d: %s", index, _hr_text(exc.hresult))
            continue
        for slot in range(count):
            try:
                control = sessions.GetSession(slot).QueryInterface(IAudioSessionControl2)
                pid = control.GetProcessId()
                if pid == 0 or control.IsSystemSoundsSession() == 0:  # S_OK = system sounds
                    continue
                found.append((pid, control.GetState() == _SESSION_STATE_ACTIVE))
            except COMError as exc:
                log.debug("skipping audio session %d: %s", slot, _hr_text(exc.hresult))
    return found


# --- Capture ---


@dataclass(frozen=True)
class _Format:
    name: str
    tag: int
    bits: int
    dtype: type


# Process-loopback clients can't report a mix format, so we ask for one; AUTOCONVERTPCM
# makes the audio engine convert whatever the app plays into it.
_FORMATS = (
    _Format("float32", _WAVE_FORMAT_IEEE_FLOAT, 32, np.float32),
    _Format("int16", _WAVE_FORMAT_PCM, 16, np.int16),
)


class _StopSignal:
    """Win32 event that stop() can set from any thread and the capture thread closes."""

    def __init__(self) -> None:
        self.handle = _kernel32.CreateEventW(None, True, False, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._lock = threading.Lock()

    def set(self) -> None:
        with self._lock:
            if self.handle:
                _kernel32.SetEvent(self.handle)

    def close(self) -> None:
        with self._lock:
            handle, self.handle = self.handle, None
        if handle:
            _kernel32.CloseHandle(handle)


def _activate_client(pid: int, mode: int):
    """ActivateAudioInterfaceAsync on the process-loopback virtual device -> IAudioClient."""
    params = _ActivationParams(
        _ACTIVATION_TYPE_PROCESS_LOOPBACK, _ProcessLoopbackParams(pid, mode)
    )
    prop = _PropVariantBlob(vt=_VT_BLOB)
    prop.blob.cbSize = sizeof(params)
    prop.blob.pBlobData = ctypes.addressof(params)
    handler = _ActivationHandler()
    operation = POINTER(IActivateAudioInterfaceAsyncOperation)()
    hr = _ActivateAudioInterfaceAsync(
        _VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        byref(IAudioClient._iid_),
        byref(prop),
        handler,
        byref(operation),
    )
    if hr < 0:
        raise ProcessLoopbackError(f"ActivateAudioInterfaceAsync failed: {_hr_text(hr)}", hr)
    if not handler.done.wait(_ACTIVATE_TIMEOUT_S):
        raise ProcessLoopbackError("process-loopback activation timed out")
    activate_hr, unknown = _call("GetActivateResult", operation.GetActivateResult)
    if activate_hr < 0:
        raise ProcessLoopbackError(
            f"process-loopback activation failed: {_hr_text(activate_hr)}", activate_hr
        )
    return unknown.QueryInterface(IAudioClient)


class _Stream:
    """A started process-loopback capture client. Lives and dies on the capture thread."""

    def __init__(self, pid: int, mode: int, rate: int, channels: int) -> None:
        self.rate = rate
        self.channels = channels
        self.client = self.capture = self.event = None
        self._next_qpc: int | None = None  # expected timestamp (100 ns units) of next packet
        rejected = []
        for fmt in _FORMATS:
            client = _activate_client(pid, mode)
            align = channels * fmt.bits // 8
            wfx = _WaveFormatEx(fmt.tag, channels, rate, rate * align, align, fmt.bits, 0)
            try:
                client.Initialize(_SHAREMODE_SHARED, _STREAMFLAGS, _BUFFER_HNS, 0, byref(wfx), None)
            except COMError as exc:
                rejected.append(f"{fmt.name}: {_hr_text(exc.hresult)}")
                hr = exc.hresult
                continue
            self.client, self.format = client, fmt
            break
        else:
            raise ProcessLoopbackError(
                f"IAudioClient::Initialize refused {rate} Hz x{channels} ({'; '.join(rejected)})",
                hr,
            )
        try:
            self.event = _kernel32.CreateEventW(None, False, False, None)
            if not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            _call("IAudioClient::SetEventHandle", self.client.SetEventHandle, self.event)
            service = _call(
                "IAudioClient::GetService", self.client.GetService, byref(IAudioCaptureClient._iid_)
            )
            self.capture = service.QueryInterface(IAudioCaptureClient)
            del service
            _call("IAudioClient::Start", self.client.Start)
        except BaseException:
            self.close()
            raise

    def drain(self, on_audio: AudioCallback) -> int:
        """Hand every queued packet to on_audio as float32 (frames, channels).

        Returns how many frames Windows dropped because we read too late. No flag reports
        that; it shows up as a jump in the packet timestamps while a backlog was waiting."""
        capture = self.capture
        gap = packets = 0
        while capture.GetNextPacketSize():
            data, frames, flags, _position, qpc = capture.GetBuffer()
            if not frames:  # AUDCLNT_S_BUFFER_EMPTY: nothing was handed out, nothing to release
                break
            packets += 1
            duration = frames * 10_000_000 / self.rate
            if self._next_qpc is not None and qpc - self._next_qpc > duration / 2:
                gap += round((qpc - self._next_qpc) * self.rate / 10_000_000)
            self._next_qpc = qpc + round(duration)
            try:
                if flags & _BUFFERFLAGS_SILENT or not data:
                    block = np.zeros((frames, self.channels), np.float32)
                else:
                    raw = np.empty((frames, self.channels), self.format.dtype)
                    ctypes.memmove(raw.ctypes.data, data, raw.nbytes)
                    block = raw if raw.dtype == np.float32 else raw.astype(np.float32) / 32768.0
            finally:
                capture.ReleaseBuffer(frames)
            on_audio(block, self.rate)
        # A jump seen by an on-time reader (one packet waiting) is Windows realigning the
        # timeline as the app opens or closes its stream, not lost audio.
        return gap if packets > 1 else 0

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.Stop()
            except COMError:
                pass
        self.capture = self.client = None  # releases the COM objects right here
        if self.event:
            _kernel32.CloseHandle(self.event)
            self.event = None


def _exe_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, ValueError):
        return f"pid {pid}"


class ProcessLoopbackSource(AudioSource):
    """Captures what one app plays (or, with exclude=True, everything except that app).

    exclude=False captures `pid` plus its child processes; exclude=True captures all
    other audio on the PC instead. Windows (verified on build 26200) only treats the
    target and its *direct* children as "the tree", including children spawned later;
    grandchildren are not included. That covers Discord and other Chromium/Electron apps,
    whose helper processes are all children of the main process, so pass the app-root
    PID from find_app_pids() or list_audio_apps(). include_tree=False is not supported:
    Windows has no "this process only" mode.

    Audio arrives as float32 (frames, channels) at `sample_rate` (48 kHz stereo unless you
    ask for, e.g., 16000 Hz mono; the audio engine converts) in 10 ms blocks, continuously,
    as zeros while the app is quiet. In include mode the capture ends by itself when the
    target process exits: `running` turns False and an INFO line is logged.
    """

    def __init__(
        self,
        pid: int,
        include_tree: bool = True,
        exclude: bool = False,
        label: str = "",
        *,
        sample_rate: int = 48000,
        channels: int = 2,
    ) -> None:
        if not include_tree:
            raise ValueError(
                "Windows process loopback always includes the target's child processes; "
                "include_tree=False is not supported"
            )
        self.pid = int(pid)
        self.exclude = exclude
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_format = ""  # "float32" or "int16": what Windows delivers, set by start()
        self.label = label or (
            f"All apps except {_exe_name(self.pid)} [app]"
            if exclude
            else f"{_exe_name(self.pid)} [app]"
        )
        self.error: BaseException | None = None  # what ended a running capture, if anything
        self.dropped_frames = 0  # audio Windows threw away because we read it too late
        self._mode = _MODE_EXCLUDE_TREE if exclude else _MODE_INCLUDE_TREE
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_signal: _StopSignal | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, on_audio: AudioCallback) -> None:
        """Start capturing; raises ProcessLoopbackError if it can't."""
        if not is_supported():
            raise ProcessLoopbackError(
                f"per-app capture needs Windows 11 (build {MIN_WINDOWS_BUILD}+); "
                f"this is build {sys.getwindowsversion().build}"
            )
        # Windows happily "captures" a PID that doesn't exist (silence forever), so check.
        if not psutil.pid_exists(self.pid):
            raise ProcessLoopbackError(f"process {self.pid} is not running")
        with self._lock:
            if self.running:
                raise RuntimeError(f"{self.label} is already capturing")
            self._ready.clear()
            self._start_error = self.error = None
            self.dropped_frames = 0
            signal = _StopSignal()
            thread = threading.Thread(
                target=self._run,
                args=(on_audio, signal),
                name=f"process-loopback-{self.pid}",
                daemon=True,
            )
            self._thread, self._stop_signal = thread, signal
            thread.start()
        if not self._ready.wait(_START_TIMEOUT_S):
            self.stop()
            raise ProcessLoopbackError(f"timed out starting capture of {self.label}")
        if self._start_error is not None:
            error, self._start_error = self._start_error, None
            self.stop()  # joins the thread, which has already cleaned up
            raise error

    def stop(self) -> None:
        with self._lock:
            thread, signal = self._thread, self._stop_signal
            self._thread = self._stop_signal = None
        if thread is None:
            return
        signal.set()
        if thread is not threading.current_thread():  # stop() from inside on_audio is fine
            thread.join(_STOP_TIMEOUT_S)
            if thread.is_alive():
                log.warning("%s: capture thread is still busy after stop()", self.label)

    def _run(self, on_audio: AudioCallback, signal: _StopSignal) -> None:
        hr_init = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        # Windows keeps only ~30 ms queued, so outrank a CPU-hungry game for a core.
        mmcss = _avrt.AvSetMmThreadCharacteristicsW("Audio", byref(wintypes.DWORD(0)))
        stream = None
        try:
            try:
                stream = _Stream(self.pid, self._mode, self.sample_rate, self.channels)
                self.sample_format = stream.format.name
            except BaseException as exc:  # noqa: BLE001 - re-raised by start()
                _drop_frame_locals(exc)
                self._start_error = exc
                return
            finally:
                self._ready.set()
            log.info(
                "%s: capturing pid %d (%s mode), %d Hz x%d %s",
                self.label,
                self.pid,
                "exclude" if self.exclude else "include",
                self.sample_rate,
                self.channels,
                self.sample_format,
            )
            try:
                self._capture(stream, on_audio, signal)
            except Exception as exc:
                _drop_frame_locals(exc)
                self.error = exc
                log.exception("%s: capture stopped by an error", self.label)
        finally:
            if stream is not None:
                stream.close()
            stream = None
            signal.close()
            if mmcss:
                _avrt.AvRevertMmThreadCharacteristics(mmcss)
            if hr_init >= 0:
                _ole32.CoUninitialize()

    def _capture(self, stream: _Stream, on_audio: AudioCallback, signal: _StopSignal) -> None:
        process = None
        if self._mode == _MODE_INCLUDE_TREE:  # nothing left to hear once the target is gone
            process = _kernel32.OpenProcess(_SYNCHRONIZE, False, self.pid)
        handles = [signal.handle] + ([process] if process else []) + [stream.event]
        wait_on = (wintypes.HANDLE * len(handles))(*handles)
        next_drop_log = 0.0
        try:
            while True:
                result = _kernel32.WaitForMultipleObjects(len(handles), wait_on, False, _POLL_MS)
                if result == _WAIT_OBJECT_0:
                    return
                if result == _WAIT_FAILED:
                    raise ctypes.WinError(ctypes.get_last_error())
                dropped = stream.drain(on_audio)
                if dropped:
                    self.dropped_frames += dropped
                    if time.monotonic() >= next_drop_log:
                        next_drop_log = time.monotonic() + _DROP_LOG_INTERVAL_S
                        log.warning(
                            "%s: capture fell behind; %.0f ms of audio lost so far",
                            self.label,
                            1000 * self.dropped_frames / stream.rate,
                        )
                if process and result == _WAIT_OBJECT_0 + 1:
                    log.info("%s: process %d exited; capture ended", self.label, self.pid)
                    return
        finally:
            if process:
                _kernel32.CloseHandle(process)
