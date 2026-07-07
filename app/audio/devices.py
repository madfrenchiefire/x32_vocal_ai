"""Audio device enumeration.

Read-only discovery of available PortAudio devices via sounddevice, so the
user can choose which sound card is the input device and which is the
output device -- they need not be the same device -- before
app.audio.engine.AudioEngine opens a stream on top of a fixed assumption.
Side-effect-free: no audio callback, no stream opened, no real-time
constraint.

**ASIO is required, not just preferred.** The X-USB card's Windows driver
exposes both an ASIO device and one or more MME/WDM/WASAPI "wrapped"
devices for the same physical hardware -- picking the wrong one breaks a
core assumption elsewhere in this app: app.audio.engine.AudioEngine and
app.osc.routing_apply both treat "Card slot N" as the same thing as
"channel index N-1 of the opened audio stream," and that 1:1 channel
correspondence is only guaranteed under ASIO. A non-ASIO wrapper can
remap, downmix, or otherwise not expose a stable direct channel order.
list_input_devices()/list_output_devices() therefore filter to ASIO-hosted
devices by default (asio_only=True) -- a machine with no ASIO driver
installed sees an empty list rather than a device that would silently
break the channel/slot mapping.
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
    is_asio: bool = False


def list_audio_devices() -> list[AudioDevice]:
    """Every device PortAudio can see, input- and output-capable alike, ASIO
    or not -- use list_input_devices()/list_output_devices() instead unless
    you specifically need the unfiltered list (e.g. find_device_by_name
    looking up whatever's already configured, ASIO or not)."""
    if sd is None:
        raise RuntimeError("PortAudio is not available on this system") from _IMPORT_ERROR
    host_apis = sd.query_hostapis()
    devices = []
    for index, info in enumerate(sd.query_devices()):
        host_api_name = host_apis[info["hostapi"]]["name"]
        devices.append(
            AudioDevice(
                index=index,
                name=info["name"],
                host_api=host_api_name,
                max_input_channels=info["max_input_channels"],
                max_output_channels=info["max_output_channels"],
                default_sample_rate=info["default_samplerate"],
                is_asio=host_api_name == "ASIO",
            )
        )
    return devices


def list_input_devices(asio_only: bool = True) -> list[AudioDevice]:
    devices = [d for d in list_audio_devices() if d.max_input_channels > 0]
    return [d for d in devices if d.is_asio] if asio_only else devices


def list_output_devices(asio_only: bool = True) -> list[AudioDevice]:
    devices = [d for d in list_audio_devices() if d.max_output_channels > 0]
    return [d for d in devices if d.is_asio] if asio_only else devices


def find_device_by_name(name: str, devices: list[AudioDevice] | None = None) -> AudioDevice | None:
    devices = devices if devices is not None else list_audio_devices()
    for device in devices:
        if device.name == name:
            return device
    return None
