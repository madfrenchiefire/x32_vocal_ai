"""Application entrypoint: wires OSC, MIDI, the audio engine, the crash
watchdog, and the Flask/WebSocket web UI into one running process.

Usage:
    python -m app.main [--config config.json]

Every service is best-effort at startup rather than fatal: CLAUDE.md's
device-setup panel exists precisely so a first run with nothing configured
yet (console_ip / audio devices / MIDI ports all None) still comes up and
lets the user choose devices and a console from the web UI. A configured
service that fails to start (console unreachable, device disappeared,
etc.) is logged and skipped rather than aborting the whole process --
whatever *is* available still comes up, per CLAUDE.md's gig-safe spirit.

Filter banks and echo cancellers are provisioned for all 32 console
channels up front, keyed by console channel number -- the insert-based
routing reads each managed channel off its own Card-input index, so the
bank that processes channel N is simply filter_banks[N]. The audio
stream's channel count is fixed for the life of the process; which
channels are actually looped through the app is decided at "Apply Routing"
time in the web UI, so every channel's bank needs to be ready in advance.
"""
from __future__ import annotations

import argparse
import sys

from app.audio.echo_cancellation import EchoCanceller
from app.audio.engine import AudioEngine, AudioEngineError
from app.audio.filters import NotchFilterBank
from app.config import AppConfig, load_config
from app.diagnostics.logger import DiagnosticsLogger
from app.midi.service import MidiService
from app.osc.assign_set import snapshot_assign_sets
from app.osc.console_eq_sync import ConsoleEqSync
from app.licensing.manager import LicenseManager
from app.licensing.online import LicenseRefresher, OnlineLicenseClient
from app.licensing.public_key import PUBLIC_KEY_HEX
from app.licensing.store import LicenseStore
from app.osc.gain_assist import GainAssist
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError
from app.osc.routing_apply import RoutingApplyError, bypass_channel
from app.state import AppState
from app.watchdog import Watchdog
from app.web.server import run as run_web

NUM_CHANNELS = 32


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Run the X32 SonicSniper app (OSC + MIDI + audio + web UI).",
    )
    parser.add_argument("--config", default=None, help="Path to config.json (default: ./config.json if present)")
    return parser


def _start_osc(config: AppConfig, diagnostics: DiagnosticsLogger, state: AppState) -> OscConnection | None:
    if config.console_ip is None:
        print("No console_ip configured -- use the web UI's Console Setup panel to search for one or enter its IP.")
        return None

    osc = OscConnection(
        host=config.console_ip,
        port=config.console_port,
        diagnostics=diagnostics,
        state=state,
        timeout_sec=config.osc_timeout_sec,
        xremote_interval_sec=config.xremote_interval_sec,
        min_firmware=config.min_firmware,
        reconnect_backoff_sec=config.reconnect_backoff_sec,
    )
    try:
        osc.connect()
        print(f"Connected to console: {osc.xinfo}")
    except (OscConnectionError, FirmwareTooOldError) as exc:
        print(f"WARNING: could not connect to console at startup: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="startup console connect failed")
        return None

    try:
        state.set_assign_set_snapshot(snapshot_assign_sets(osc, diagnostics))
    except Exception as exc:
        # Non-fatal -- routing snapshot/apply still works without this;
        # the crash watchdog just won't have Set A/B to restore.
        diagnostics.log_error(exc, context="startup assign-set snapshot failed")

    return osc


def _start_midi(
    config: AppConfig, diagnostics: DiagnosticsLogger, state: AppState, osc: OscConnection | None
) -> MidiService | None:
    if config.midi_input_port is None:
        print("No midi_input_port configured -- MIDI control surface unavailable until set up in the web UI.")
        return None

    midi_service = MidiService(config=config, diagnostics=diagnostics, state=state, osc=osc)

    def _on_ai_toggle(channel: int, enabled: bool) -> None:
        state.channels[channel].ai_enabled = enabled
        diagnostics.log_state_change("ai_toggled", after={"channel": channel, "enabled": enabled, "source": "midi"})

    def _on_sensitivity_change(channel: int, value: float) -> None:
        state.channels[channel].sensitivity = value

    def _on_insert_bypass_toggle(channel: int) -> None:
        if osc is None or state.current_snapshot is None:
            diagnostics.log_error(
                RoutingApplyError("insert/bypass pressed with no console connection or snapshot"),
                context="midi_insert_bypass_toggle",
            )
            return
        try:
            bypass_channel(osc, diagnostics, channel, state.current_snapshot, state)
        except RoutingApplyError as exc:
            diagnostics.log_error(exc, context="midi_insert_bypass_toggle")

    midi_service.on_ai_toggle = _on_ai_toggle
    midi_service.on_sensitivity_change = _on_sensitivity_change
    midi_service.on_insert_bypass_toggle = _on_insert_bypass_toggle

    try:
        midi_service.start()
        print(f"MIDI service started on {config.midi_input_port!r}.")
        return midi_service
    except Exception as exc:
        print(f"WARNING: MIDI service failed to start: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="startup MIDI start failed")
        return None


def _start_audio(config: AppConfig, diagnostics: DiagnosticsLogger, state: AppState) -> AudioEngine | None:
    if config.audio_input_device is None or config.audio_output_device is None:
        print("Audio devices not configured -- audio engine unavailable until set up in the web UI.")
        return None

    filter_banks = {
        channel: NotchFilterBank(
            sample_rate=config.audio_sample_rate,
            max_notches=config.max_notches_per_channel,
            depth_db=config.notch_depth_db,
            q=config.notch_q,
        )
        for channel in range(1, NUM_CHANNELS + 1)
    }
    echo_cancellers = (
        {
            channel: EchoCanceller(filter_length_taps=config.echo_filter_length_taps)
            for channel in range(1, NUM_CHANNELS + 1)
        }
        if config.echo_cancellation_enabled
        else {}
    )

    audio_engine = AudioEngine(
        config=config,
        diagnostics=diagnostics,
        filter_banks=filter_banks,
        echo_cancellers=echo_cancellers,
        state=state,
    )
    try:
        audio_engine.start()
        print(f"Audio engine started: {config.audio_input_device!r} -> {config.audio_output_device!r}.")
        return audio_engine
    except AudioEngineError as exc:
        print(f"WARNING: audio engine failed to start: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="startup audio start failed")
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config_path = args.config or "config.json"
    config = load_config(config_path)

    state = AppState()
    diagnostics = DiagnosticsLogger(
        log_dir=config.resolved_log_dir(),
        ring_buffer_size=config.ring_buffer_size,
        state_provider=state.summary,
    )
    diagnostics.log_user_action("app_started", {"config_path": config_path})

    # Licensing gate: a valid key or an active trial is required to run the
    # real services. When neither holds, the web server still comes up (so
    # the user can enter a key on the License screen) but OSC/MIDI/audio are
    # not started -- and the API is blocked by app.web.routes' license gate.
    license_store = LicenseStore()
    license_manager = LicenseManager(store=license_store, product_id=config.product_id)
    # Online mode (config.license_mode == "online"): activate against the
    # license server and re-verify periodically in the background. Offline
    # mode (default) uses pasted vendor-signed keys, no client/refresher.
    online_client = None
    license_refresher = None
    if config.license_mode == "online" and config.license_server_url:
        online_client = OnlineLicenseClient(config, license_store, diagnostics, PUBLIC_KEY_HEX)
        license_refresher = LicenseRefresher(online_client, diagnostics)
        license_refresher.start()
    license_status = license_manager.evaluate_at_startup(diagnostics)
    if license_status.functional:
        osc = _start_osc(config, diagnostics, state)
        midi_service = _start_midi(config, diagnostics, state, osc)
        audio_engine = _start_audio(config, diagnostics, state)
    else:
        print(
            f"\n*** {license_status.message} ***\n"
            f"    Machine code: {license_status.machine_code}\n"
            f"    Open the web UI and enter a license key to enable all features "
            f"(then restart the app).\n",
            file=sys.stderr,
        )
        osc = midi_service = audio_engine = None

    # Opt-in last-resort preamp trim (app.osc.gain_assist): created even
    # when disabled/unconnected so the web UI can toggle it and
    # /api/console/connect can rewire its OSC handle live, mirroring the
    # MidiService/watchdog pattern. request_trim is a no-op while
    # disabled, so the engine hook is always safe to wire.
    gain_assist = GainAssist(osc=osc, diagnostics=diagnostics, config=config, state=state)
    gain_assist.start()
    if audio_engine is not None:
        audio_engine.on_notch_bank_saturated = gain_assist.request_trim

    # Internal-EQ mode (app.osc.console_eq_sync): for channels set to
    # "internal" the app writes detected feedback notches into the console's
    # own EQ instead of processing the audio itself. Created always (osc
    # rewired live on connect, like the others); the engine hook enqueues
    # a sync whenever an internal-EQ channel's notch set changes.
    def _notches_for(channel: int) -> list[dict]:
        if audio_engine is None:
            return []
        bank = audio_engine.filter_banks.get(channel)
        return bank.active_notches() if bank is not None else []

    console_eq_sync = ConsoleEqSync(
        osc=osc, diagnostics=diagnostics, state=state, notches_provider=_notches_for
    )
    console_eq_sync.start()
    if audio_engine is not None:
        audio_engine.on_internal_eq_update = console_eq_sync.request_sync

    # Armed unconditionally, even with osc=None on a first run with no
    # console configured yet -- app.web.routes' /api/console/connect
    # reassigns watchdog.osc once the user searches for or manually enters
    # a console in the web UI, and Watchdog itself is a no-op restore
    # (logged, not crashed) if triggered while osc is still None.
    watchdog = Watchdog(
        osc=osc,
        diagnostics=diagnostics,
        state=state,
        snapshot_provider=lambda: state.current_snapshot,
        assign_set_snapshot_provider=lambda: state.assign_set_snapshot,
    )
    watchdog.start()

    try:
        print(f"Starting web UI on http://{config.web_host}:{config.web_port}")
        run_web(
            config, state, diagnostics,
            osc=osc, audio_engine=audio_engine, midi_service=midi_service, config_path=config_path,
            watchdog=watchdog, gain_assist=gain_assist, console_eq_sync=console_eq_sync,
            license_manager=license_manager, license_online_client=online_client,
        )
    finally:
        watchdog.trigger_full_restore(reason="clean_shutdown")
        watchdog.stop()
        gain_assist.stop()
        if license_refresher is not None:
            license_refresher.stop()
        # Clean shutdown leaves the console stock (gig-safe principle #3), so
        # put back any channel's console EQ the internal-EQ mode was managing.
        # (A *crash* deliberately keeps the notches -- see the watchdog: mid-
        # crash, live feedback suppression is safer than the engineer's
        # original EQ, same call as gain assist.)
        if osc is not None:
            console_eq_sync.restore_all()
        console_eq_sync.stop()
        if audio_engine is not None:
            audio_engine.stop()
        if midi_service is not None:
            midi_service.stop()
        if osc is not None:
            osc.close()
        diagnostics.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
