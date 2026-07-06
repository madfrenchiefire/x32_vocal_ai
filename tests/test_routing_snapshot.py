from __future__ import annotations

import json

from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot, load_snapshot, read_routing_snapshot, save_snapshot


def _populate_fake_routing(fake_x32) -> None:
    fake_x32.extra_responses[addresses.USERROUT_IN] = tuple(range(addresses.NUM_USERROUT_IN))
    fake_x32.extra_responses[addresses.USERROUT_OUT] = tuple(range(addresses.NUM_USERROUT_OUT))
    fake_x32.extra_responses[addresses.ROUTING_REC] = ("REC",)
    fake_x32.extra_responses[addresses.ROUTING_IN] = ("AN1-8", "AN9-16", "AN17-24", "AN25-32", "AUX1-4")
    fake_x32.extra_responses[addresses.ROUTING_AES50A] = (
        "OUT1-8", "OUT9-16", "OUT1-8", "OUT9-16", "P161-8", "P169-16",
    )
    fake_x32.extra_responses[addresses.ROUTING_AES50B] = (
        "OUT1-8", "OUT9-16", "OUT1-8", "OUT9-16", "P161-8", "P169-16",
    )
    fake_x32.extra_responses[addresses.ROUTING_CARD] = ("AN1-8", "AN9-16", "AN17-24", "AN25-32")
    fake_x32.extra_responses[addresses.ROUTING_OUT] = ("OUT1-4", "OUT5-8", "OUT9-12", "OUT13-16")
    fake_x32.extra_responses[addresses.ROUTING_PLAY] = (
        "CARD1-8", "CARD9-16", "CARD17-24", "CARD25-32", "AUX1-4",
    )


def test_read_routing_snapshot_captures_confirmed_addresses(fake_x32, diagnostics, app_state):
    _populate_fake_routing(fake_x32)

    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    try:
        snapshot = read_routing_snapshot(osc, diagnostics, name="test_snapshot")
    finally:
        osc.close()

    assert snapshot.name == "test_snapshot"
    assert snapshot.routing_addresses_verified is True
    assert len(snapshot.userrout_in) == addresses.NUM_USERROUT_IN
    assert len(snapshot.userrout_out) == addresses.NUM_USERROUT_OUT
    assert snapshot.userrout_in[0] == 0
    assert snapshot.userrout_out[-1] == addresses.NUM_USERROUT_OUT - 1
    assert snapshot.routing["rec"] == ["REC"]
    assert snapshot.routing["in"] == ["AN1-8", "AN9-16", "AN17-24", "AN25-32", "AUX1-4"]
    assert snapshot.routing["card"] == ["AN1-8", "AN9-16", "AN17-24", "AN25-32"]


def test_read_routing_snapshot_handles_missing_replies(fake_x32, diagnostics, app_state):
    # No extra_responses registered -- every block query times out. Should
    # not raise; missing data is reported as None, not fatal.
    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=0.3,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    try:
        snapshot = read_routing_snapshot(osc, diagnostics, name="empty_snapshot")
    finally:
        osc.close()

    assert snapshot.userrout_in is None
    assert snapshot.userrout_out is None
    assert all(v is None for v in snapshot.routing.values())


def test_save_and_load_snapshot_round_trip(tmp_path):
    snapshot = RoutingSnapshot(
        schema_version=2,
        created_at="2026-01-01T00:00:00.000Z",
        name="roundtrip",
        console={"model": "X32"},
        userrout_in=[0] * addresses.NUM_USERROUT_IN,
        userrout_out=[0] * addresses.NUM_USERROUT_OUT,
        routing={"rec": ["REC"], "in": ["AN1-8", "AN9-16", "AN17-24", "AN25-32", "AUX1-4"]},
    )
    path = save_snapshot(snapshot, tmp_path)
    assert path.name == "roundtrip.json"

    loaded = load_snapshot(path)
    assert loaded == snapshot

    raw = json.loads(path.read_text())
    assert len(raw["userrout_in"]) == addresses.NUM_USERROUT_IN
    assert len(raw["userrout_out"]) == addresses.NUM_USERROUT_OUT
