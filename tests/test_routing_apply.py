from __future__ import annotations

import pytest

from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_apply import RoutingApplyError, apply_routing, bypass_channel, restore_snapshot
from app.osc.routing_snapshot import RoutingSnapshot


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
    # 4 blocks + 1 AUX slot, matching addresses.ROUTING_GROUPS["in"]'s shape.
    routing_in = [0, 1, 0, 0, 0]  # block 1-8: AN1-8, block 9-16: AN9-16
    return RoutingSnapshot(
        schema_version=3,
        created_at="2026-01-01T00:00:00.000Z",
        name="test_snapshot",
        console={"model": "X32"},
        userrout_in=[0] * addresses.NUM_USERROUT_IN,
        userrout_out=[0] * addresses.NUM_USERROUT_OUT,
        routing={"in": routing_in},
    )


def test_apply_routing_inserts_channel_and_flips_block(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (0,)
    fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[0]] = (0,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        assignments = apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()

    assert assignments == {1: 1}
    assert fake_x32.extra_responses[addresses.userrout_in_addr(1)] == (129,)  # Card 1
    assert fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[0]] == (20,)  # User In 1-8
    assert app_state.channels[1].inserted is True
    assert app_state.channels[1].card_out_slot == 1


def test_apply_routing_preserves_other_channels_sharing_the_block(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (0,)
    fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[1]] = (1,)  # block 9-16 = AN9-16

    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        apply_routing(osc, diagnostics, [9], snapshot, app_state)
    finally:
        osc.close()

    # Channel 9 -> Card 1.
    assert fake_x32.extra_responses[addresses.userrout_in_addr(9)] == (129,)
    # Channels 10-16 (sharing the same block, not selected) keep their
    # original physical source (Local Analog 10-16) replicated via userrout.
    for ch in range(10, 17):
        assert fake_x32.extra_responses[addresses.userrout_in_addr(ch)] == (ch,)
    assert fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[1]] == (21,)  # User In 9-16


def test_apply_routing_reuses_existing_card_slot(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (0,)
    for addr in addresses.ROUTING_IN_BLOCKS:
        fake_x32.extra_responses[addr] = (0,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        first = apply_routing(osc, diagnostics, [1], snapshot, app_state)
        second = apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()

    assert first == second == {1: 1}


def test_apply_routing_raises_on_mismatch(monkeypatch, fake_x32, diagnostics, app_state):
    # Pre-seed fixed values, then connect and only *afterward* neuter send()
    # so writes become silent no-ops -- simulates a write that never takes
    # effect, without breaking the initial /xinfo handshake (which also
    # goes through send()).
    fake_x32.extra_responses[addresses.userrout_in_addr(1)] = (0,)
    fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[0]] = (0,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(osc, "send", lambda *a, **k: None)

    snapshot = _make_snapshot()
    try:
        with pytest.raises(RoutingApplyError):
            apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()


def test_bypass_channel_toggles_insert_and_restore(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (0,)
    fake_x32.extra_responses[addresses.ROUTING_IN_BLOCKS[0]] = (0,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        apply_routing(osc, diagnostics, [1], snapshot, app_state)
        assert app_state.channels[1].inserted is True

        now_inserted = bypass_channel(osc, diagnostics, 1, snapshot, app_state)
        assert now_inserted is False
        assert fake_x32.extra_responses[addresses.userrout_in_addr(1)] == (0,)  # back to snapshot value

        now_inserted = bypass_channel(osc, diagnostics, 1, snapshot, app_state)
        assert now_inserted is True
        assert fake_x32.extra_responses[addresses.userrout_in_addr(1)] == (129,)  # re-inserted, same Card slot
    finally:
        osc.close()


def test_bypass_channel_without_prior_insert_raises(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        with pytest.raises(RoutingApplyError):
            bypass_channel(osc, diagnostics, 5, snapshot, app_state)
    finally:
        osc.close()


def test_restore_snapshot_replays_all_values(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_IN + addresses.ALL_USERROUT_OUT:
        fake_x32.extra_responses[addr] = (99,)
    for group, pairs in addresses.ROUTING_GROUPS.items():
        for addr, _table in pairs:
            fake_x32.extra_responses[addr] = (99,)

    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = RoutingSnapshot(
        schema_version=3,
        created_at="2026-01-01T00:00:00.000Z",
        name="restore_test",
        console={"model": "X32"},
        userrout_in=list(range(addresses.NUM_USERROUT_IN)),
        userrout_out=list(range(addresses.NUM_USERROUT_OUT)),
        routing={"routswitch": [0], "card": [0, 1, 2, 3]},
    )
    app_state.channels[1].inserted = True
    app_state.channels[1].card_out_slot = 5
    try:
        mismatches = restore_snapshot(osc, diagnostics, snapshot, app_state)
    finally:
        osc.close()

    assert mismatches == []
    assert fake_x32.extra_responses[addresses.userrout_in_addr(1)] == (0,)
    assert fake_x32.extra_responses[addresses.userrout_in_addr(32)] == (31,)
    assert fake_x32.extra_responses[addresses.ROUTING_ROUTSWITCH] == (0,)
    assert app_state.channels[1].inserted is False
    assert app_state.channels[1].card_out_slot is None
