from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.audio.devices import AudioDevice
from app.audio.echo_cancellation import MAIN_LR_USERROUT_OUT_VALUE
from app.audio.engine import AudioEngine
from app.audio.filters import NotchFilterBank
from app.config import AppConfig, load_config
from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot
from app.web.server import create_app


def _make_osc(fake_x32, diagnostics, app_state) -> OscConnection:
    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    return osc


def _make_snapshot() -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        created_at="2026-01-01T00:00:00.000Z",
        name="web_test_snapshot",
        console={"model": "X32"},
        userrout_in=[0] * addresses.NUM_USERROUT_IN,
        userrout_out=[0] * addresses.NUM_USERROUT_OUT,
        routing={},
    )


def _app(tmp_path, app_state, diagnostics, **kwargs):
    config = AppConfig(log_dir=str(tmp_path / "logs"), snapshot_dir=str(tmp_path / "snapshots"))
    app, socketio = create_app(config, app_state, diagnostics, **kwargs)
    return app, socketio, config


# -- device setup -------------------------------------------------------------


def test_list_devices_reports_enumeration_and_selection(monkeypatch, tmp_path, app_state, diagnostics):
    app, _sio, config = _app(tmp_path, app_state, diagnostics)
    config.audio_input_device = "Interface In"

    fake_device = AudioDevice(
        index=0, name="Interface In", host_api="ASIO", max_input_channels=2, max_output_channels=0,
        default_sample_rate=48000.0, is_asio=True,
    )
    monkeypatch.setattr("app.web.routes.audio_devices.list_input_devices", lambda: [fake_device])
    monkeypatch.setattr("app.web.routes.audio_devices.list_output_devices", lambda: [])
    monkeypatch.setattr("app.web.routes.midi_devices.list_midi_input_ports", lambda: ["X-USB MIDI 1"])
    monkeypatch.setattr("app.web.routes.midi_devices.list_midi_output_ports", lambda: ["X-USB MIDI 1"])

    client = app.test_client()
    response = client.get("/api/devices")
    assert response.status_code == 200
    data = response.get_json()
    assert data["audio_inputs"] == [
        {"index": 0, "name": "Interface In", "host_api": "ASIO", "max_input_channels": 2,
         "max_output_channels": 0, "default_sample_rate": 48000.0, "is_asio": True}
    ]
    assert data["midi_inputs"] == ["X-USB MIDI 1"]
    assert data["selected"]["audio_input_device"] == "Interface In"


def test_list_devices_reports_audio_error_gracefully(monkeypatch, tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)

    def _raise():
        raise RuntimeError("PortAudio is not available on this system")

    monkeypatch.setattr("app.web.routes.audio_devices.list_input_devices", lambda: _raise())
    monkeypatch.setattr("app.web.routes.audio_devices.list_output_devices", lambda: _raise())
    monkeypatch.setattr("app.web.routes.midi_devices.list_midi_input_ports", lambda: [])
    monkeypatch.setattr("app.web.routes.midi_devices.list_midi_output_ports", lambda: [])

    client = app.test_client()
    data = client.get("/api/devices").get_json()
    assert data["audio_inputs"] == []
    assert "PortAudio" in data["audio_error"]


def test_select_devices_updates_config_and_persists(tmp_path, app_state, diagnostics):
    config_path = tmp_path / "config.json"
    app, _sio, config = _app(tmp_path, app_state, diagnostics, config_path=str(config_path))

    client = app.test_client()
    response = client.post(
        "/api/devices/select",
        data=json.dumps({"audio_input_device": "Interface In", "midi_input_port": "X-USB MIDI 1"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert config.audio_input_device == "Interface In"
    assert config.midi_input_port == "X-USB MIDI 1"

    reloaded = load_config(config_path)
    assert reloaded.audio_input_device == "Interface In"


def test_select_devices_without_config_path_does_not_write_to_disk(tmp_path, app_state, diagnostics):
    app, _sio, config = _app(tmp_path, app_state, diagnostics)  # no config_path
    client = app.test_client()

    client.post(
        "/api/devices/select",
        data=json.dumps({"audio_input_device": "Interface In"}),
        content_type="application/json",
    )
    assert config.audio_input_device == "Interface In"
    assert not (tmp_path / "config.json").exists()


# -- console setup -------------------------------------------------------------


def test_search_console_returns_discovered_list(monkeypatch, tmp_path, app_state, diagnostics):
    from app.osc.discovery import DiscoveredConsole

    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    monkeypatch.setattr(
        "app.web.routes.discover_consoles",
        lambda **kwargs: [DiscoveredConsole(host="10.10.0.142", port=10023, name="TESTX32", model="X32", version="4.13")],
    )

    client = app.test_client()
    response = client.post("/api/console/search", json={})
    assert response.status_code == 200
    assert response.get_json()["consoles"] == [
        {"host": "10.10.0.142", "port": 10023, "name": "TESTX32", "model": "X32", "version": "4.13"}
    ]


def test_console_status_reports_disconnected_by_default(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    data = client.get("/api/console/status").get_json()
    assert data["connected"] is False


def test_connect_console_succeeds_and_updates_config(fake_x32, tmp_path, app_state, diagnostics):
    config_path = tmp_path / "config.json"
    app, _sio, config = _app(tmp_path, app_state, diagnostics, config_path=str(config_path))
    client = app.test_client()

    response = client.post("/api/console/connect", json={"host": "127.0.0.1", "port": fake_x32.port})
    assert response.status_code == 200
    data = response.get_json()
    assert data["connected"] is True
    assert config.console_ip == "127.0.0.1"
    assert config.console_port == fake_x32.port

    status = client.get("/api/console/status").get_json()
    assert status["connected"] is True

    reloaded = load_config(config_path)
    assert reloaded.console_ip == "127.0.0.1"

    app.extensions["osc_connection"].close()


def test_connect_console_requires_host(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    response = client.post("/api/console/connect", json={})
    assert response.status_code == 400


def test_connect_console_failure_returns_502_and_clears_extension(tmp_path, app_state, diagnostics):
    app, _sio, config = _app(tmp_path, app_state, diagnostics)
    config.osc_timeout_sec = 0.2
    client = app.test_client()

    # Nothing is listening on this port.
    probe_response = client.post("/api/console/connect", json={"host": "127.0.0.1", "port": 1})
    assert probe_response.status_code == 502
    assert app.extensions["osc_connection"] is None


def test_connect_console_closes_previous_connection_and_rewires_dependents(fake_x32, tmp_path, app_state, diagnostics):
    midi_service = MagicMock()
    watchdog = MagicMock()
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, midi_service=midi_service, watchdog=watchdog)
    client = app.test_client()

    response = client.post("/api/console/connect", json={"host": "127.0.0.1", "port": fake_x32.port})
    assert response.status_code == 200
    new_osc = app.extensions["osc_connection"]
    assert midi_service.osc is new_osc
    assert watchdog.osc is new_osc

    previous_osc = MagicMock()
    previous_osc.connected = True
    app.extensions["osc_connection"] = previous_osc

    response = client.post("/api/console/connect", json={"host": "127.0.0.1", "port": fake_x32.port})
    assert response.status_code == 200
    previous_osc.close.assert_called_once()

    app.extensions["osc_connection"].close()


def test_disconnect_console_closes_and_clears_dependents(fake_x32, tmp_path, app_state, diagnostics):
    midi_service = MagicMock()
    watchdog = MagicMock()
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, midi_service=midi_service, watchdog=watchdog)
    client = app.test_client()
    client.post("/api/console/connect", json={"host": "127.0.0.1", "port": fake_x32.port})

    response = client.post("/api/console/disconnect")
    assert response.status_code == 200
    assert response.get_json()["connected"] is False
    assert app.extensions["osc_connection"] is None
    assert midi_service.osc is None
    assert watchdog.osc is None


# -- channels -------------------------------------------------------------


def test_list_channels_returns_all_32(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    data = client.get("/api/channels").get_json()
    assert len(data["channels"]) == 32
    assert data["channels"][0]["index"] == 1


def test_toggle_ai_updates_channel_state(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()

    response = client.post(
        "/api/channels/3/ai_toggle", data=json.dumps({"enabled": True}), content_type="application/json"
    )
    assert response.status_code == 200
    assert app_state.channels[3].ai_enabled is True


def test_toggle_ai_out_of_range_channel_404s(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    response = client.post(
        "/api/channels/99/ai_toggle", data=json.dumps({"enabled": True}), content_type="application/json"
    )
    assert response.status_code == 404


def test_update_channel_settings_applies_overrides_and_live_bank(tmp_path, app_state, diagnostics):
    engine = AudioEngine(
        config=AppConfig(),
        diagnostics=diagnostics,
        filter_banks={5: NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0)},
        detector=MagicMock(),
    )
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, audio_engine=engine)
    client = app.test_client()

    response = client.post(
        "/api/channels/5/settings",
        data=json.dumps({"max_notches": 4, "notch_depth_db": -6.0, "notch_q": 5.0, "mode": "ring_out"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert app_state.channels[5].max_notches_override == 4
    assert app_state.channels[5].mode == "ring_out"
    bank = engine.filter_banks[5]
    assert bank.max_notches == 4
    assert bank.default_depth_db == -6.0
    assert bank.default_q == 5.0


def test_update_channel_settings_rejects_bad_mode(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    response = client.post(
        "/api/channels/5/settings", data=json.dumps({"mode": "bogus"}), content_type="application/json"
    )
    assert response.status_code == 400


# -- routing panel: requires a live OSC connection -----------------------------


def test_routing_endpoints_503_without_osc(tmp_path, app_state, diagnostics):
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)  # no osc
    client = app.test_client()

    assert client.post("/api/routing/snapshot").status_code == 503
    assert client.post("/api/routing/apply", json={"channels": [1]}).status_code == 503
    assert client.post("/api/routing/restore").status_code == 503
    assert client.post("/api/channels/1/bypass").status_code == 503


def test_apply_and_restore_require_snapshot_first(fake_x32, tmp_path, app_state, diagnostics):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        response = client.post("/api/routing/apply", json={"channels": [1]})
        assert response.status_code == 400
        response = client.post("/api/routing/restore")
        assert response.status_code == 400
    finally:
        osc.close()


def test_take_snapshot_reads_and_stores(fake_x32, tmp_path, app_state, diagnostics):
    for addr in addresses.ALL_USERROUT_IN + addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        response = client.post("/api/routing/snapshot")
        assert response.status_code == 200
        assert app_state.current_snapshot is not None
    finally:
        osc.close()


def test_apply_routing_endpoint_inserts_channel(fake_x32, tmp_path, app_state, diagnostics):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (0,)
    fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[0]] = (0,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app_state.set_snapshot(_make_snapshot())
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        response = client.post("/api/routing/apply", json={"channels": [1]})
        assert response.status_code == 200
        assert response.get_json()["assignments"] == {"1": 1}
        assert app_state.channels[1].inserted is True
    finally:
        osc.close()


def test_apply_routing_endpoint_requires_channels(fake_x32, tmp_path, app_state, diagnostics):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app_state.set_snapshot(_make_snapshot())
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        response = client.post("/api/routing/apply", json={"channels": []})
        assert response.status_code == 400
    finally:
        osc.close()


def test_bypass_and_bypass_all_endpoints(fake_x32, tmp_path, app_state, diagnostics):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (0,)
    fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[0]] = (0,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app_state.set_snapshot(_make_snapshot())
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        client.post("/api/routing/apply", json={"channels": [1]})

        response = client.post("/api/channels/1/bypass")
        assert response.status_code == 200
        assert response.get_json()["inserted"] is False
        assert app_state.channels[1].inserted is False

        response = client.post("/api/routing/bypass_all", json={"bypass": False})
        assert response.status_code == 200
        assert app_state.channels[1].inserted is True
    finally:
        osc.close()


def test_restore_endpoint_replays_snapshot(fake_x32, tmp_path, app_state, diagnostics):
    for addr in addresses.ALL_USERROUT_IN + addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (99,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app_state.set_snapshot(_make_snapshot())  # everything 0
    app, _sio, _config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        response = client.post("/api/routing/restore")
        assert response.status_code == 200
        assert response.get_json()["mismatches"] == []
        assert fake_x32.extra_responses[addresses.userrout_in_addr(1)] == (0,)
    finally:
        osc.close()


# -- echo cancellation --------------------------------------------------------


def test_echo_cancellation_toggle_routes_reference(fake_x32, tmp_path, app_state, diagnostics):
    for addr in addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (0,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    app, _sio, config = _app(tmp_path, app_state, diagnostics, osc=osc)
    client = app.test_client()
    try:
        response = client.post("/api/echo_cancellation/toggle", json={"enabled": True})
        assert response.status_code == 200
        data = response.get_json()
        assert data["reference_card_channels"] == [1, 2]
        assert config.echo_cancellation_enabled is True
        assert fake_x32.extra_responses[addresses.userrout_out_addr(1)] == (MAIN_LR_USERROUT_OUT_VALUE,)
    finally:
        osc.close()


def test_echo_cancellation_toggle_without_osc_503s_and_reverts(tmp_path, app_state, diagnostics):
    app, _sio, config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    response = client.post("/api/echo_cancellation/toggle", json={"enabled": True})
    assert response.status_code == 503
    assert config.echo_cancellation_enabled is False


def test_echo_cancellation_disable_does_not_require_osc(tmp_path, app_state, diagnostics):
    app, _sio, config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    response = client.post("/api/echo_cancellation/toggle", json={"enabled": False})
    assert response.status_code == 200
    assert config.echo_cancellation_enabled is False


# -- diagnostics / websocket ----------------------------------------------------


def test_recent_events_endpoint(tmp_path, app_state, diagnostics):
    diagnostics.log_state_change("something_happened")
    app, _sio, _config = _app(tmp_path, app_state, diagnostics)
    client = app.test_client()
    data = client.get("/api/events?limit=5").get_json()
    assert any(e["payload"]["description"] == "something_happened" for e in data["events"])


def test_diagnostics_events_broadcast_over_websocket(tmp_path, app_state, diagnostics):
    app, socketio, _config = _app(tmp_path, app_state, diagnostics)
    test_client = socketio.test_client(app)
    test_client.get_received()  # drain the connect-time event

    diagnostics.log_state_change("ws_test_event", after={"foo": "bar"})

    received = test_client.get_received()
    assert any(
        msg["name"] == "diagnostics_event" and msg["args"][0]["payload"]["description"] == "ws_test_event"
        for msg in received
    )
