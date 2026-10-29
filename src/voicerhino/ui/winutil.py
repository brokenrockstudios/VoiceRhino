"""Win32 bits Qt doesn't cover: re-asserting topmost, and a global hotkey."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000


def raise_topmost(widget: QWidget) -> None:
    """Put the window back on top without activating it (games and other
    always-on-top windows can push it down)."""
    user32.SetWindowPos(int(widget.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Ctrl+Alt+C' -> (modifiers, virtual-key code)."""
    mods, vk = 0, 0
    for part in (p.strip().lower() for p in text.split("+") if p.strip()):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "meta"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"unsupported key {part!r} in hotkey {text!r}")
    if not vk:
        raise ValueError(f"hotkey {text!r} has no key")
    return mods, vk


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "VoiceRhino"


def _launch_command() -> str:
    """The windowless launcher created by `uv sync` for the gui-script entry point."""
    import sys
    from pathlib import Path

    gui = Path(sys.prefix) / "Scripts" / "voicerhino-gui.exe"
    if gui.exists():
        return f'"{gui}"'
    return f'"{Path(sys.executable).with_name("pythonw.exe")}" -m voicerhino'


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Start VoiceRhino when this Windows user signs in (per-user, no admin)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_NAME)
            except FileNotFoundError:
                pass


class GlobalHotkey(QAbstractNativeEventFilter):
    """System-wide hotkey delivered to a hidden native window."""

    _next_id = 1

    def __init__(self, callback: Callable[[], None]):
        super().__init__()
        self._callback = callback
        self._window = QWidget()  # never shown; winId() gives it an HWND
        self._hwnd = int(self._window.winId())
        self._id = GlobalHotkey._next_id
        GlobalHotkey._next_id += 1
        self._registered = False
        QCoreApplication.instance().installNativeEventFilter(self)

    def register(self, text: str) -> bool:
        self.unregister()
        try:
            mods, vk = parse_hotkey(text)
        except ValueError:
            log.warning("bad hotkey %r", text)
            return False
        self._registered = bool(user32.RegisterHotKey(self._hwnd, self._id, mods | MOD_NOREPEAT, vk))
        if not self._registered:
            log.warning("hotkey %s is taken by another app", text)
        return self._registered

    def unregister(self) -> None:
        if self._registered:
            user32.UnregisterHotKey(self._hwnd, self._id)
            self._registered = False

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self._callback()
                return True, 0
        return False, 0
