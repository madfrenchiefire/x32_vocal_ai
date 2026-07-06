from __future__ import annotations

import json

from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot, load_snapshot, read_routing_snapshot, save_snapshot


def _populate_fake_userrout(fake_x32) -> None:
    for ch in range(1, addresses.NUM_CHANNELS + 1):
        fake_x32.extra_responses[addresses.userrout_in(ch)] = (ch,)
        fake_x32.extra_responses[addresses.userrout_out(ch)] = (ch + 100,)


def test_read_routing_snapshot_captures_confirmed_addresses(fake_x32, diagnostics, app_state):
    _populate_fake_userrout(fake_x32)
    # Deliberately leave the block-level (unverified) addresses unanswered,
    # matching how a real console would behave if those paths are wrong.

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
    assert snapshot.routing_addresses_verified is False
    assert len(snapshot.userrout_in) == 32
    assert len(snapshot.userrout_out) == 32
    assert snapshot.userrout_in[1] == [1]
    assert snapshot.userrout_out[32] == [132]
    # Unverified block addresses got no reply from the fake console.
    assert all(v is None for v in snapshot.routing_in_blocks.values())
    assert all(v is None for v in snapshot.card_out_blocks.values())


def test_save_and_load_snapshot_round_trip(tmp_path):
    snapshot = RoutingSnapshot(
        schema_version=1,
        created_at="2026-01-01T00:00:00.000Z",
        name="roundtrip",
        console={"model": "X32"},
        userrout_in={1: [1], 2: [2]},
        userrout_out={1: [101], 2: [102]},
        routing_in_blocks={"/config/routing/IN/1-8": None},
        card_out_blocks={"/config/routing/OUT/CARD/1-8": None},
    )
    path = save_snapshot(snapshot, tmp_path)
    assert path.name == "roundtrip.json"

    loaded = load_snapshot(path)
    assert loaded == snapshot

    # Confirm channel numbers survive the JSON round trip as ints, not str.
    raw = json.loads(path.read_text())
    assert set(raw["userrout_in"].keys()) == {"1", "2"}
    assert set(loaded.userrout_in.keys()) == {1, 2}
