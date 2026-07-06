"""Audio device enumeration.

Read-only discovery of available PortAudio devices via sounddevice, so the
user can choose which sound card is the input device and which is the
output device -- they need not be the same device -- before the real-time
audio engine (app.audio.engine, not yet implemented) is built on top of a
fixed assumption. Implemented now (unlike the rest of app.audio) because
it's side-effect-free: no audio callback, no stream opened, no real-time
constraint.
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    import sounddevice as sd
    _IMPORT_ERROR: OSError | None = None
except OSError as exc:
    # sounddevice raises at import time (not call time) if the native
    # PortAudio library isn't installed on this machine -- defer that
    # failure to first use so importing this module never crashes the
    # caller (e.g. the CLI, which reports it as a normal error).
    sd = None
    _IMPORT_ERROR = exc


@dataclass
class AudioDevice:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float


def list_audio_devices() -> list[AudioDevice]:
    """Every device PortAudio can see, input- and output-capable alike."""
    if sd is None:
        raise RuntimeError("PortAudio is not available on this system") from _IMPORT_ERROR
    host_apis = sd.query_hostapis()
    devices = []
    for index, info in enumerate(sd.query_devices()):
        devices.append(
            AudioDevice(
                index=index,
                name=info["name"],
                host_api=host_apis[info["hostapi"]]["name"],
                max_input_channels=info["max_input_channels"],
                max_output_channels=info["max_output_channels"],
                default_sample_rate=info["default_samplerate"],
            )
        )
    return devices


def list_input_devices() -> list[AudioDevice]:
    return [d for d in list_audio_devices() if d.max_input_channels > 0]


def list_output_devices() -> list[AudioDevice]:
    return [d for d in list_audio_devices() if d.max_output_channels > 0]


def find_device_by_name(name: str, devices: list[AudioDevice] | None = None) -> AudioDevice | None:
    devices = devices if devices is not None else list_audio_devices()
    for device in devices:
        if device.name == name:
            return device
    return None
