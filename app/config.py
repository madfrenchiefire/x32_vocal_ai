"""Application configuration.

Central, typed configuration for every service (OSC, MIDI, audio, web). A
single :class:`AppConfig` instance is constructed at startup and passed down
to whichever services are active, so new modules should accept config as a
constructor argument rather than reading files or env vars themselves.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.json")


@dataclass
class AppConfig:
    # --- OSC / console connection ---
    console_ip: str | None = None
    console_port: int = 10023
    local_osc_port: int = 0  # 0 = OS-assigned ephemeral port
    osc_timeout_sec: float = 2.0
    xremote_interval_sec: float = 8.0
    min_firmware: str = "4.0"
    reconnect_backoff_sec: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    # --- MIDI (Phase 2, not yet implemented) ---
    midi_channel: int = 16
    # Port names as reported by app.midi.devices.list_midi_input_ports() /
    # list_midi_output_ports() (mido). None = not yet chosen -- the MIDI
    # service must not guess a device, per the device-selection requirement.
    midi_input_port: str | None = None
    midi_output_port: str | None = None

    # --- Audio engine (Phase 3+, not yet implemented) ---
    audio_sample_rate: int = 48000
    audio_block_size: int = 128
    max_notches_per_channel: int = 12
    notch_depth_db: float = -12.0
    # Device names as reported by app.audio.devices.list_input_devices() /
    # list_output_devices() (sounddevice). None = not yet chosen -- the
    # audio engine must not assume "the X-USB card" is the only option.
    audio_input_device: str | None = None
    audio_output_device: str | None = None

    # --- Diagnostics ---
    ring_buffer_size: int = 10_000
    log_dir: str = "logs"
    snapshot_dir: str = "snapshots"

    # --- Web ---
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir)

    def resolved_snapshot_dir(self) -> Path:
        return Path(self.snapshot_dir)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config from a JSON file, falling back to defaults for any
    field not present. Missing file is not an error -- it just means
    "use defaults" (e.g. first run before a config.json exists).
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return AppConfig()

    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    defaults = asdict(AppConfig())
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ValueError(f"Unknown config key(s) in {config_path}: {sorted(unknown)}")

    defaults.update(raw)
    if isinstance(defaults.get("reconnect_backoff_sec"), list):
        defaults["reconnect_backoff_sec"] = tuple(defaults["reconnect_backoff_sec"])
    return AppConfig(**defaults)


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, indent=2, default=list)
    return config_path
