"""HTTP routes.

Device setup, routing panel (snapshot/apply/restore/bypass), per-channel
settings, and the echo-cancellation toggle described in CLAUDE.md's
"Web UI" section, plus the diagnostics export endpoint. Per-channel
spectrum display and live meters need the (still-unconfirmed) /meters
blob layout and aren't wired up here.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from app.audio import devices as audio_devices
from app.audio.echo_cancellation import EchoCancellationError, auto_route_reference_signal
from app.config import save_config
from app.diagnostics.export import build_debug_bundle
from app.midi import devices as midi_devices
from app.osc.assign_set import snapshot_assign_sets
from app.osc.channel_eq import ChannelEqError, commit_notches_to_console_eq, restore_console_eq
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError
from app.osc.discovery import discover_consoles
from app.osc.routing_apply import RoutingApplyError, apply_routing, bypass_channel, restore_snapshot
from app.osc.routing_snapshot import read_routing_snapshot, save_snapshot
from app.osc.scribble_strip import read_all_channel_configs

bp = Blueprint("main", __name__)


@bp.route("/")
def index() -> str:
    return render_template("index.html")


# -- helpers ---------------------------------------------------------------


def _osc_or_error():
    osc = current_app.extensions.get("osc_connection")
    if osc is None or not osc.connected:
        return None, (jsonify(error="not connected to the console"), 503)
    return osc, None


def _snapshot_or_error(state):
    if state.current_snapshot is None:
        return None, (jsonify(error="no routing snapshot captured yet -- take one first"), 400)
    return state.current_snapshot, None


def _channel_dict(state, channel: int) -> dict:
    d = dataclasses.asdict(state.channels[channel])
    d["has_console_eq_snapshot"] = channel in state.console_eq_snapshots
    return d


# -- device setup ------------------------------------------------------------


@bp.route("/api/devices")
def list_devices():
    config = current_app.extensions["app_config"]

    try:
        audio_inputs = [dataclasses.asdict(d) for d in audio_devices.list_input_devices()]
        audio_outputs = [dataclasses.asdict(d) for d in audio_devices.list_output_devices()]
        audio_error = None
        if not audio_inputs and not audio_outputs:
            audio_error = (
                "No ASIO audio devices found. Card slot numbers must map 1:1 to the audio "
                "interface's channels, which only ASIO guarantees -- install/enable the X-USB "
                "card's ASIO driver (or whichever ASIO driver your interface provides)."
            )
    except RuntimeError as exc:
        audio_inputs, audio_outputs, audio_error = [], [], str(exc)

    try:
        midi_inputs = midi_devices.list_midi_input_ports()
        midi_outputs = midi_devices.list_midi_output_ports()
        midi_error = None
    except Exception as exc:  # mido/rtmidi backend issues vary by platform
        midi_inputs, midi_outputs, midi_error = [], [], str(exc)

    return jsonify(
        audio_inputs=audio_inputs,
        audio_outputs=audio_outputs,
        audio_error=audio_error,
        midi_inputs=midi_inputs,
        midi_outputs=midi_outputs,
        midi_error=midi_error,
        selected={
            "audio_input_device": config.audio_input_device,
            "audio_output_device": config.audio_output_device,
            "midi_input_port": config.midi_input_port,
            "midi_output_port": config.midi_output_port,
        },
    )


@bp.route("/api/devices/select", methods=["POST"])
def select_devices():
    config = current_app.extensions["app_config"]
    diagnostics = current_app.extensions["diagnostics"]
    body = request.get_json(force=True, silent=True) or {}

    correlation_id = diagnostics.log_user_action("select_devices", body)
    for field in ("audio_input_device", "audio_output_device", "midi_input_port", "midi_output_port"):
        if field in body:
            setattr(config, field, body[field])
    config_path = current_app.extensions.get("config_path")
    if config_path is not None:
        save_config(config, config_path)
    diagnostics.log_state_change(
        "devices_selected",
        after={
            "audio_input_device": config.audio_input_device,
            "audio_output_device": config.audio_output_device,
            "midi_input_port": config.midi_input_port,
            "midi_output_port": config.midi_output_port,
        },
        correlation_id=correlation_id,
    )
    return jsonify(status="ok")


# -- console setup ------------------------------------------------------------


@bp.route("/api/console/search", methods=["POST"])
def search_console():
    diagnostics = current_app.extensions["diagnostics"]
    body = request.get_json(force=True, silent=True) or {}
    diagnostics.log_user_action("search_console", body)

    try:
        found = discover_consoles(
            target_address=body.get("target_address", "255.255.255.255"),
            port=int(body.get("port", 10023)),
            timeout_sec=float(body.get("timeout_sec", 2.0)),
        )
    except OSError as exc:
        return jsonify(error=str(exc)), 500

    return jsonify(consoles=[dataclasses.asdict(c) for c in found])


@bp.route("/api/console/status")
def console_status():
    config = current_app.extensions["app_config"]
    osc = current_app.extensions.get("osc_connection")
    return jsonify(
        connected=osc is not None and osc.connected,
        console_ip=config.console_ip,
        console_port=config.console_port,
        xinfo=osc.xinfo if osc is not None else {},
    )


@bp.route("/api/console/connect", methods=["POST"])
def connect_console():
    config = current_app.extensions["app_config"]
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    body = request.get_json(force=True, silent=True) or {}

    host = body.get("host")
    if not host:
        return jsonify(error="host is required"), 400
    port = int(body.get("port", config.console_port))

    correlation_id = diagnostics.log_user_action("connect_console", {"host": host, "port": port})

    previous_osc = current_app.extensions.get("osc_connection")
    if previous_osc is not None:
        previous_osc.close()

    osc = OscConnection(
        host=host,
        port=port,
        diagnostics=diagnostics,
        state=state,
        timeout_sec=config.osc_timeout_sec,
        xremote_interval_sec=config.xremote_interval_sec,
        min_firmware=config.min_firmware,
        reconnect_backoff_sec=config.reconnect_backoff_sec,
    )
    try:
        osc.connect(correlation_id=correlation_id)
    except (OscConnectionError, FirmwareTooOldError) as exc:
        diagnostics.log_error(exc, context="connect_console failed", correlation_id=correlation_id)
        current_app.extensions["osc_connection"] = None
        return jsonify(error=str(exc)), 502

    current_app.extensions["osc_connection"] = osc
    config.console_ip = host
    config.console_port = port
    config_path = current_app.extensions.get("config_path")
    if config_path is not None:
        save_config(config, config_path)

    midi_service = current_app.extensions.get("midi_service")
    if midi_service is not None:
        midi_service.osc = osc
    watchdog = current_app.extensions.get("watchdog")
    if watchdog is not None:
        watchdog.osc = osc

    try:
        state.set_assign_set_snapshot(snapshot_assign_sets(osc, diagnostics, correlation_id=correlation_id))
    except Exception as exc:
        # Non-fatal -- routing still works without this; the crash
        # watchdog just won't have Set A/B to restore.
        diagnostics.log_error(exc, context="connect_console assign-set snapshot failed", correlation_id=correlation_id)

    # Scribble-strip name/color reads (32 paced queries) are deliberately
    # NOT done here -- they'd add several seconds to this endpoint's
    # response. The web UI instead calls POST /api/channels/refresh_names
    # itself right after a successful connect (see index.html), so this
    # response comes back promptly and names populate a moment later.
    return jsonify(connected=True, xinfo=osc.xinfo)


@bp.route("/api/console/disconnect", methods=["POST"])
def disconnect_console():
    diagnostics = current_app.extensions["diagnostics"]
    diagnostics.log_user_action("disconnect_console")

    osc = current_app.extensions.get("osc_connection")
    if osc is not None:
        osc.close()
    current_app.extensions["osc_connection"] = None

    midi_service = current_app.extensions.get("midi_service")
    if midi_service is not None:
        midi_service.osc = None
    watchdog = current_app.extensions.get("watchdog")
    if watchdog is not None:
        watchdog.osc = None

    return jsonify(connected=False)


# -- state / channels --------------------------------------------------------


@bp.route("/api/state")
def get_state():
    state = current_app.extensions["app_state"]
    config = current_app.extensions["app_config"]
    return jsonify(state=state.summary(), config=config.to_dict())


@bp.route("/api/channels")
def list_channels():
    state = current_app.extensions["app_state"]
    return jsonify(channels=[_channel_dict(state, ch) for ch in range(1, 33)])


@bp.route("/api/channels/refresh_names", methods=["POST"])
def refresh_channel_names():
    """Re-reads scribble-strip name/color for all 32 channels from the
    console (e.g. after the user renames channels there post-connect) and
    returns the updated grid. See app.osc.scribble_strip.read_all_channel_configs."""
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    osc, error = _osc_or_error()
    if error:
        return error

    correlation_id = diagnostics.log_user_action("refresh_channel_names")
    state.apply_channel_configs(read_all_channel_configs(osc, correlation_id=correlation_id))
    return jsonify(channels=[_channel_dict(state, ch) for ch in range(1, 33)])


@bp.route("/api/channels/meters")
def channel_meters():
    """Synchronous fallback for a page load before the WebSocket
    "channel_meters" broadcast has fired yet -- keyed by Card slot number,
    same as the live push (see app.web.sockets)."""
    audio_engine = current_app.extensions.get("audio_engine")
    if audio_engine is None:
        return jsonify(levels={})
    return jsonify(levels=audio_engine.get_levels())


@bp.route("/api/channels/<int:channel>/ai_toggle", methods=["POST"])
def toggle_ai(channel: int):
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    if channel not in state.channels:
        return jsonify(error=f"channel {channel} out of range"), 404

    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled", False))
    channel_state = state.channels[channel]
    before = channel_state.ai_enabled
    channel_state.ai_enabled = enabled
    diagnostics.log_state_change(
        "ai_toggled", before={"channel": channel, "enabled": before}, after={"channel": channel, "enabled": enabled}
    )
    return jsonify(channel=_channel_dict(state, channel))


@bp.route("/api/channels/<int:channel>/echo_toggle", methods=["POST"])
def toggle_channel_echo_cancellation(channel: int):
    """Per-channel opt-in, independent of the global
    AppConfig.echo_cancellation_enabled toggle (/api/echo_cancellation/toggle)
    -- that one turns the feature on and routes the reference signal;
    this one decides which channels' EchoCanceller actually runs."""
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    if channel not in state.channels:
        return jsonify(error=f"channel {channel} out of range"), 404

    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled", False))
    channel_state = state.channels[channel]
    before = channel_state.echo_cancellation_enabled
    channel_state.echo_cancellation_enabled = enabled
    diagnostics.log_state_change(
        "echo_cancellation_toggled",
        before={"channel": channel, "enabled": before},
        after={"channel": channel, "enabled": enabled},
    )
    return jsonify(channel=_channel_dict(state, channel))


@bp.route("/api/channels/<int:channel>/eq/commit", methods=["POST"])
def commit_channel_eq(channel: int):
    """Burn the channel's active app notches into the console's own 4-band
    EQ (app.osc.channel_eq), so the ring-out result persists with the app
    out of the audio path. Snapshots the console EQ first; the snapshot is
    kept for /eq/restore even if the commit partially fails."""
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    audio_engine = current_app.extensions.get("audio_engine")
    if channel not in state.channels:
        return jsonify(error=f"channel {channel} out of range"), 404
    osc, error = _osc_or_error()
    if error:
        return error

    card_slot = state.channels[channel].card_out_slot
    bank = audio_engine.filter_banks.get(card_slot) if (audio_engine is not None and card_slot is not None) else None
    notches = bank.active_notches() if bank is not None else []
    if not notches:
        return jsonify(error="channel has no active notches to commit"), 400

    correlation_id = diagnostics.log_user_action(
        "commit_console_eq", {"channel": channel, "notch_count": len(notches)}
    )
    try:
        result = commit_notches_to_console_eq(osc, diagnostics, channel, notches, correlation_id=correlation_id)
    except ChannelEqError as exc:
        partial_snapshot = getattr(exc, "snapshot", None)
        if partial_snapshot is not None:
            state.set_console_eq_snapshot(channel, partial_snapshot)
        return jsonify(
            error=str(exc),
            restore_available=partial_snapshot is not None,
        ), 500

    state.set_console_eq_snapshot(channel, result["snapshot"])
    return jsonify(written=result["written"], channel=_channel_dict(state, channel))


@bp.route("/api/channels/<int:channel>/eq/restore", methods=["POST"])
def restore_channel_eq(channel: int):
    """Put the channel's console EQ back exactly as it was before the last
    /eq/commit for it."""
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    if channel not in state.channels:
        return jsonify(error=f"channel {channel} out of range"), 404
    snapshot = state.console_eq_snapshots.get(channel)
    if snapshot is None:
        return jsonify(error="no console EQ snapshot for this channel -- nothing to restore"), 400
    osc, error = _osc_or_error()
    if error:
        return error

    correlation_id = diagnostics.log_user_action("restore_console_eq", {"channel": channel})
    restore_console_eq(osc, diagnostics, channel, snapshot, correlation_id=correlation_id)
    return jsonify(restored=True, channel=_channel_dict(state, channel))


@bp.route("/api/channels/<int:channel>/settings", methods=["POST"])
def update_channel_settings(channel: int):
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    audio_engine = current_app.extensions.get("audio_engine")
    if channel not in state.channels:
        return jsonify(error=f"channel {channel} out of range"), 404

    body = request.get_json(force=True, silent=True) or {}
    channel_state = state.channels[channel]

    if "sensitivity" in body:
        channel_state.sensitivity = float(body["sensitivity"])
    if "max_notches" in body:
        channel_state.max_notches_override = int(body["max_notches"])
    if "notch_depth_db" in body:
        channel_state.notch_depth_db_override = float(body["notch_depth_db"])
    if "notch_q" in body:
        channel_state.notch_q_override = float(body["notch_q"])
    if "mode" in body:
        if body["mode"] not in ("live", "ring_out"):
            return jsonify(error="mode must be 'live' or 'ring_out'"), 400
        channel_state.mode = body["mode"]

    if audio_engine is not None and channel_state.card_out_slot is not None:
        # filter_banks is keyed by Card slot number, NOT console channel
        # number -- apply_routing can assign a channel to any free slot,
        # so these differ whenever card_out_slot != channel. Using
        # `channel` directly here would silently edit some other
        # channel's filter bank whenever that happened.
        bank = audio_engine.filter_banks.get(channel_state.card_out_slot)
        if bank is not None:
            if channel_state.max_notches_override is not None:
                bank.max_notches = channel_state.max_notches_override
            if channel_state.notch_depth_db_override is not None:
                bank.default_depth_db = channel_state.notch_depth_db_override
            if channel_state.notch_q_override is not None:
                bank.default_q = channel_state.notch_q_override

    diagnostics.log_state_change("channel_settings_updated", after={"channel": channel, **body})
    return jsonify(channel=_channel_dict(state, channel))


# -- routing panel ------------------------------------------------------------


@bp.route("/api/routing/snapshot", methods=["POST"])
def take_snapshot():
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    config = current_app.extensions["app_config"]
    osc, error = _osc_or_error()
    if error:
        return error

    correlation_id = diagnostics.log_user_action("take_routing_snapshot")
    body = request.get_json(force=True, silent=True) or {}
    snapshot = read_routing_snapshot(osc, diagnostics, name=body.get("name"), correlation_id=correlation_id)
    path = save_snapshot(snapshot, config.resolved_snapshot_dir())
    state.set_snapshot(snapshot, str(path))
    return jsonify(name=snapshot.name, path=str(path))


@bp.route("/api/routing/apply", methods=["POST"])
def apply_routing_route():
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    osc, error = _osc_or_error()
    if error:
        return error
    snapshot, error = _snapshot_or_error(state)
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    channels = body.get("channels", [])
    if not channels:
        return jsonify(error="no channels selected"), 400

    correlation_id = diagnostics.log_user_action("apply_routing", {"channels": channels})
    try:
        assignments = apply_routing(osc, diagnostics, channels, snapshot, state, correlation_id=correlation_id)
    except RoutingApplyError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(assignments=assignments)


@bp.route("/api/routing/restore", methods=["POST"])
def restore_route():
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    osc, error = _osc_or_error()
    if error:
        return error
    snapshot, error = _snapshot_or_error(state)
    if error:
        return error

    correlation_id = diagnostics.log_user_action("restore_snapshot")
    mismatches = restore_snapshot(osc, diagnostics, snapshot, state, correlation_id=correlation_id)
    return jsonify(mismatches=mismatches)


@bp.route("/api/channels/<int:channel>/bypass", methods=["POST"])
def bypass_route(channel: int):
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    osc, error = _osc_or_error()
    if error:
        return error
    snapshot, error = _snapshot_or_error(state)
    if error:
        return error
    if channel not in state.channels:
        return jsonify(error=f"channel {channel} out of range"), 404

    correlation_id = diagnostics.log_user_action("bypass_channel", {"channel": channel})
    try:
        inserted = bypass_channel(osc, diagnostics, channel, snapshot, state, correlation_id=correlation_id)
    except RoutingApplyError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(channel=channel, inserted=inserted)


@bp.route("/api/routing/bypass_all", methods=["POST"])
def bypass_all_route():
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    osc, error = _osc_or_error()
    if error:
        return error
    snapshot, error = _snapshot_or_error(state)
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    bypass = bool(body.get("bypass", True))
    correlation_id = diagnostics.log_user_action("bypass_all", {"bypass": bypass})

    results: dict[int, bool] = {}
    errors: dict[int, str] = {}
    for channel, channel_state in state.channels.items():
        if channel_state.card_out_slot is None:
            continue  # never inserted this session -- nothing to bypass/restore
        target_inserted = not bypass
        if channel_state.inserted == target_inserted:
            continue
        try:
            results[channel] = bypass_channel(osc, diagnostics, channel, snapshot, state, correlation_id=correlation_id)
        except RoutingApplyError as exc:
            errors[channel] = str(exc)

    return jsonify(results=results, errors=errors)


# -- echo cancellation --------------------------------------------------------


@bp.route("/api/echo_cancellation/toggle", methods=["POST"])
def toggle_echo_cancellation():
    config = current_app.extensions["app_config"]
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled", False))

    correlation_id = diagnostics.log_user_action("toggle_echo_cancellation", {"enabled": enabled})
    config.echo_cancellation_enabled = enabled

    if enabled and config.echo_reference_card_channels is None:
        osc, error = _osc_or_error()
        if error:
            config.echo_cancellation_enabled = False
            return error
        try:
            slots = auto_route_reference_signal(osc, diagnostics, config, state, correlation_id=correlation_id)
        except EchoCancellationError as exc:
            config.echo_cancellation_enabled = False
            return jsonify(error=str(exc)), 500
        return jsonify(enabled=True, reference_card_channels=list(slots))

    return jsonify(enabled=enabled, reference_card_channels=list(config.echo_reference_card_channels or []))


@bp.route("/api/echo_cancellation/reference", methods=["POST"])
def set_echo_reference():
    """Explicit Card slot picker, independent of the enable toggle --
    lets the user choose which two Card outputs get the console's Main
    L/R bus, rather than always auto-picking the first two free slots."""
    config = current_app.extensions["app_config"]
    state = current_app.extensions["app_state"]
    diagnostics = current_app.extensions["diagnostics"]
    body = request.get_json(force=True, silent=True) or {}

    card_channels = body.get("card_channels")
    if not isinstance(card_channels, list) or len(card_channels) != 2:
        return jsonify(error="card_channels must be a list of exactly 2 Card slot numbers"), 400
    try:
        left, right = int(card_channels[0]), int(card_channels[1])
    except (TypeError, ValueError):
        return jsonify(error="card_channels must be two integers"), 400
    if left == right:
        return jsonify(error="left and right must be different Card slots"), 400

    osc, error = _osc_or_error()
    if error:
        return error

    correlation_id = diagnostics.log_user_action("set_echo_reference", {"card_channels": [left, right]})
    try:
        slots = auto_route_reference_signal(
            osc, diagnostics, config, state, correlation_id=correlation_id, card_channels=(left, right)
        )
    except EchoCancellationError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(reference_card_channels=list(slots))


# -- diagnostics ---------------------------------------------------------------


@bp.route("/api/events")
def recent_events():
    diagnostics = current_app.extensions["diagnostics"]
    limit = request.args.get("limit", default=100, type=int)
    return jsonify(events=diagnostics.get_recent(limit))


@bp.route("/api/diagnostics/export", methods=["POST"])
def export_debug_bundle():
    diagnostics = current_app.extensions["diagnostics"]
    state = current_app.extensions["app_state"]
    config = current_app.extensions["app_config"]
    osc = current_app.extensions.get("osc_connection")

    correlation_id = diagnostics.log_user_action("export_debug_bundle")

    bundles_dir = config.resolved_log_dir() / "bundles"
    filename = f"debug_bundle_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    output_path = bundles_dir / filename

    connection_info = dict(osc.xinfo) if osc is not None else {}
    try:
        build_debug_bundle(output_path, diagnostics, state, config, connection_info=connection_info)
    except Exception as exc:
        diagnostics.log_error(exc, context="export_debug_bundle failed", correlation_id=correlation_id)
        raise

    diagnostics.log_state_change(
        "debug_bundle_exported", after={"path": str(output_path)}, correlation_id=correlation_id
    )
    return send_file(output_path, as_attachment=True, download_name=filename)
