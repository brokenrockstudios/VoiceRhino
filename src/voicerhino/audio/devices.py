"""Enumerate WASAPI capture options: microphones/inputs and a loopback for every output.

Devices are stored in config by key ("loopback:<name>", "input:<name>") rather
than by index, because PortAudio indices shift when devices come and go.
"""

from __future__ import annotations

from dataclasses import dataclass

LOOPBACK_SUFFIX = " [Loopback]"
DEFAULT_OUTPUT_KEY = "loopback:@default"  # follows whatever Windows' default output is


@dataclass(frozen=True)
class DeviceInfo:
    key: str
    name: str
    kind: str  # "loopback" or "input"
    index: int
    channels: int
    rate: int
    is_default: bool

    @property
    def label(self) -> str:
        if self.kind == "loopback":
            return f"{self.name} (what plays on it)"
        return f"{self.name} (input)"


def _pa():
    import pyaudiowpatch as pyaudio

    return pyaudio, pyaudio.PyAudio()


def list_devices() -> list[DeviceInfo]:
    pyaudio, pa = _pa()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        default_in = (
            pa.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]
            if wasapi["defaultInputDevice"] >= 0
            else None
        )
        devices: list[DeviceInfo] = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] <= 0:
                continue
            loopback = bool(d.get("isLoopbackDevice"))
            name = d["name"].removesuffix(LOOPBACK_SUFFIX) if loopback else d["name"]
            kind = "loopback" if loopback else "input"
            devices.append(
                DeviceInfo(
                    key=f"{kind}:{name}",
                    name=name,
                    kind=kind,
                    index=i,
                    channels=int(d["maxInputChannels"]),
                    rate=int(d["defaultSampleRate"]),
                    is_default=name == (default_out if loopback else default_in),
                )
            )
        devices.sort(key=lambda d: (d.kind != "loopback", not d.is_default, d.name.lower()))
        return devices
    finally:
        pa.terminate()


def resolve(key: str) -> DeviceInfo:
    """Find the current device for a stored key (raises LookupError if unplugged)."""
    devices = list_devices()
    if key == DEFAULT_OUTPUT_KEY:
        for d in devices:
            if d.kind == "loopback" and d.is_default:
                return d
    for d in devices:
        if d.key == key:
            return d
    raise LookupError(f"audio device not found: {key.split(':', 1)[-1]}")
