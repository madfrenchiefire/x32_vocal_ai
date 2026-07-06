from __future__ import annotations

import json

from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot, load_snapshot, read_routing_snapshot, save_snapshot


def _populate_fake_individual(fake_x32) -> None:
    for i, addr in enumerate(addresses.ALL_USERROUT_IN):
        fake_x32.extra_responses[addr] = (i,)
    for i, addr in enumerate(addresses.ALL_USERROUT_OUT):
        fake_x32.extra_responses[addr] = (i + 100,)
    for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
        for i, (addr, _table) in enumerate(addr_table_pairs):
            fake_x32.extra_responses[addr] = (i,)


def test_read_routing_snapshot_uses_individual_addresses(fake_x32, diagnostics, app_state):
    _populate_fake_individual(fake_x32)

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
    assert snapshot.userrout_in == list(range(addresses.NUM_USERROUT_IN))
    assert snapshot.userrout_out == [i + 100 for i in range(addresses.NUM_USERROUT_OUT)]
    assert snapshot.routing["card"] == [0, 1, 2, 3]
    assert snapshot.routing["aes50a"] == [0, 1, 2, 3, 4, 5]

    decoded = snapshot.decode_routing()
    assert decoded["card"] == ["AN1-8", "AN9-16", "AN17-24", "AN25-32"]
    assert decoded["routswitch"] == ["REC"]


def test_bulk_fallback_used_when_individual_query_times_out(fake_x32, diagnostics, app_state):
    _populate_fake_individual(fake_x32)
    # Simulate one dead individual node -- console should still resolve it
    # via the bulk fallback address.
    del fake_x32.extra_responses[addresses.userrout_in_addr(5)]
    fake_x32.extra_responses[addresses.USERROUT_IN] = tuple(range(addresses.NUM_USERROUT_IN))

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
        snapshot = read_routing_snapshot(osc, diagnostics, name="fallback_snapshot")
    finally:
        osc.close()

    # Filled in from the bulk array (value at index 4, i.e. channel 5).
    assert snapshot.userrout_in[4] == 4


def test_missing_data_reported_as_none_not_fatal(fake_x32, diagnostics, app_state):
    # No extra_responses registered at all -- everything times out.
    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=0.2,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    try:
        snapshot = read_routing_snapshot(osc, diagnostics, name="empty_snapshot")
    finally:
        osc.close()

    assert all(v is None for v in snapshot.userrout_in)
    assert all(v is None for v in snapshot.userrout_out)
    assert all(v is None for vals in snapshot.routing.values() for v in vals)


def test_save_and_load_snapshot_round_trip(tmp_path):
    snapshot = RoutingSnapshot(
        schema_version=3,
        created_at="2026-01-01T00:00:00.000Z",
        name="roundtrip",
        console={"model": "X32"},
        userrout_in=list(range(addresses.NUM_USERROUT_IN)),
        userrout_out=list(range(addresses.NUM_USERROUT_OUT)),
        routing={"routswitch": [0], "card": [0, 1, 2, 3]},
    )
    path = save_snapshot(snapshot, tmp_path)
    assert path.name == "roundtrip.json"

    loaded = load_snapshot(path)
    assert loaded == snapshot

    raw = json.loads(path.read_text())
    assert len(raw["userrout_in"]) == addresses.NUM_USERROUT_IN
    assert len(raw["userrout_out"]) == addresses.NUM_USERROUT_OUT
