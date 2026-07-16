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

    # --- MIDI ---
    midi_channel: int = 16
    # Port names as reported by app.midi.devices.list_midi_input_ports() /
    # list_midi_output_ports() (mido). None = not yet chosen -- the MIDI
    # service must not guess a device, per the device-selection requirement.
    midi_input_port: str | None = None
    midi_output_port: str | None = None
    # If True, selecting a 9th channel raises instead of processing it
    # without hardware controls. False (default) matches CLAUDE.md:
    # "channels beyond 8 may still be processed but are app-controlled only."
    midi_hard_cap_at_8_channels: bool = False

    # --- Audio engine ---
    audio_sample_rate: int = 48000
    audio_block_size: int = 128
    max_notches_per_channel: int = 12
    notch_depth_db: float = -12.0
    notch_q: float = 10.0
    # Device names as reported by app.audio.devices.list_input_devices() /
    # list_output_devices() (sounddevice). None = not yet chosen -- the
    # audio engine must not assume "the X-USB card" is the only option.
    audio_input_device: str | None = None
    audio_output_device: str | None = None

    # --- Echo cancellation (off by default; needs a reference signal) ---
    echo_cancellation_enabled: bool = False
    # Card channels auto-routed from the console's Main L/R bus once enabled
    # (app.audio.echo_cancellation); None = not yet assigned.
    echo_reference_card_channels: tuple[int, int] | None = None
    echo_filter_length_taps: int = 9600  # ~200ms tail at 48kHz; tune per room size

    # --- Routing writes ---
    routing_write_pace_sec: float = 0.02  # delay between paced OSC writes
    # Insert-based routing: the PC is looped into each managed channel via
    # that channel's insert point over one of the 6 Aux buses, so at most 6
    # channels can be processed through the console at once (banks of 2/4/6,
    # matching the Aux-In Card remap). Channels beyond this are app-only.
    max_insert_channels: int = 6
    # Raw /outputs/aux/NN/src value that means "Insert" (see
    # app.osc.addresses.AUX_OUT_SRC_INSERT -- not in the v4.09 doc enum, a
    # newer-firmware addition). None = unconfirmed: apply_routing then skips
    # the aux-out-src write (leaving it to be set on the desk) rather than
    # guessing. Set this once read off a real console.
    aux_out_insert_src_value: int | None = None

    # --- Preamp gain assist (app/osc/gain_assist.py) ---
    # OPT-IN last resort: when a channel's notch bank is saturated (all
    # notches busy) and detection still fires, step that channel's preamp
    # gain down. Touching gain changes the engineer's mix, so this stays
    # off unless deliberately enabled.
    gain_assist_enabled: bool = False
    gain_assist_step_db: float = 2.0  # per trim step
    gain_assist_max_total_db: float = 6.0  # hard cap per headamp per session
    gain_assist_cooldown_sec: float = 5.0  # min time between trims per channel

    # --- Console-side safety scene (app/osc/scene.py) ---
    # Scene slot (0-99) to save the console's pre-app state into before the
    # app's first routing write of a session, recallable from the desk's own
    # Scenes page even with the PC dead. None = off: scene slots hold real
    # show data, and the app must never overwrite one the user didn't
    # explicitly choose.
    safety_scene_slot: int | None = None

    # --- Diagnostics ---
    ring_buffer_size: int = 10_000
    log_dir: str = "logs"
    snapshot_dir: str = "snapshots"

    # --- Web ---
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    # --- Licensing (app.licensing) ---
    # "offline" = verify a pasted, vendor-signed key locally (no server).
    # "online"  = activate against the license server (cloud/), which binds
    #   the machine and returns a short-lived signed token the app re-checks
    #   weekly (fail-safe: keeps running if the server is merely unreachable).
    license_mode: str = "online"
    # Base URL of the deployed Cloud Functions (online mode), e.g.
    # "https://us-central1-<project>.cloudfunctions.net". The app appends
    # /activate and /check to this base.
    license_server_url: str | None = "https://us-central1-x32-sonicsniper.cloudfunctions.net"
    # This app's product id -- the license server scopes keys per product,
    # and the app rejects a token issued for a different product.
    product_id: str = "x32-sonicsniper"

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
    if isinstance(defaults.get("echo_reference_card_channels"), list):
        defaults["echo_reference_card_channels"] = tuple(defaults["echo_reference_card_channels"])
    return AppConfig(**defaults)


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, indent=2, default=list)
    return config_path
