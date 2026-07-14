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
    return RoutingSnapshot(
        schema_version=4,
        created_at="2026-01-01T00:00:00.000Z",
        name="test_snapshot",
        console={"model": "X32"},
        userrout_in=[0] * addresses.NUM_USERROUT_IN,
        userrout_out=[0] * addresses.NUM_USERROUT_OUT,
        routing={"in": [0, 0, 0, 0, 0], "card": [0, 1, 2, 3]},
    )


def test_apply_routing_inserts_channel_via_aux(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        assignments = apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()

    assert assignments == {1: 1}
    # Card output block covering channel 1 -> Local 1-8 (AN1-8 = 0).
    assert fake_x32.extra_responses[addresses.ROUTING_CARD_BLOCKS[0]] == (0,)
    # Aux In fed from Card 1-2 (1 channel rounds up to the 2-wide bank).
    assert fake_x32.extra_responses[addresses.ROUTING_IN_AUX] == (10,)
    # Channel 1 insert: POST, AUX1, on.
    assert fake_x32.extra_responses[addresses.channel_insert_pos_addr(1)] == (addresses.INSERT_POS_POST,)
    assert fake_x32.extra_responses[addresses.channel_insert_sel_addr(1)] == (17,)  # AUX1
    assert fake_x32.extra_responses[addresses.channel_insert_on_addr(1)] == (addresses.INSERT_ON,)
    # Aux-out src left untouched when the "Insert" value is unconfirmed.
    assert addresses.aux_out_src_addr(1) not in fake_x32.extra_responses
    assert app_state.channels[1].inserted is True
    assert app_state.channels[1].card_out_slot == 1


def test_apply_routing_writes_aux_out_insert_when_value_known(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        apply_routing(osc, diagnostics, [1], snapshot, app_state, insert_src_value=77)
    finally:
        osc.close()

    assert fake_x32.extra_responses[addresses.aux_out_src_addr(1)] == (77,)


def test_apply_routing_multiple_channels_assigns_banks(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        assignments = apply_routing(osc, diagnostics, [1, 5, 20], snapshot, app_state)
    finally:
        osc.close()

    assert assignments == {1: 1, 5: 2, 20: 3}
    # 3 channels -> Aux In fed from Card 1-4 (the 4-wide bank).
    assert fake_x32.extra_responses[addresses.ROUTING_IN_AUX] == (11,)
    # Card blocks covering channels 1, 5 (block 0) and 20 (block 2) set to Local.
    assert fake_x32.extra_responses[addresses.ROUTING_CARD_BLOCKS[0]] == (0,)
    assert fake_x32.extra_responses[addresses.ROUTING_CARD_BLOCKS[2]] == (2,)
    # Channel 20 uses Aux 3 (slot 3) -> insert/sel AUX3 = 19.
    assert fake_x32.extra_responses[addresses.channel_insert_sel_addr(20)] == (19,)


def test_apply_routing_reuses_existing_aux_slot(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        first = apply_routing(osc, diagnostics, [1], snapshot, app_state)
        second = apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()

    assert first == second == {1: 1}


def test_apply_routing_skips_internal_eq_channels(fake_x32, diagnostics, app_state):
    # Internal-EQ channels are never inserted; only external ones are.
    app_state.channels[2].eq_mode = "internal"
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        assignments = apply_routing(osc, diagnostics, [1, 2], snapshot, app_state)
    finally:
        osc.close()

    assert assignments == {1: 1}  # channel 2 skipped
    assert app_state.channels[2].inserted is False
    assert app_state.channels[2].card_out_slot is None
    assert addresses.channel_insert_on_addr(2) not in fake_x32.extra_responses


def test_apply_routing_all_internal_writes_nothing(fake_x32, diagnostics, app_state):
    app_state.channels[1].eq_mode = "internal"
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        assignments = apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()

    assert assignments == {}
    assert addresses.ROUTING_IN_AUX not in fake_x32.extra_responses


def test_apply_routing_too_many_channels_raises(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        with pytest.raises(RoutingApplyError, match="at most"):
            apply_routing(osc, diagnostics, [1, 2, 3, 4, 5, 6, 7], snapshot, app_state)
    finally:
        osc.close()


def test_apply_routing_raises_on_mismatch(monkeypatch, fake_x32, diagnostics, app_state):
    # Neuter send() only after the /xinfo handshake so writes become silent
    # no-ops -- simulating writes that never take effect.
    osc = _make_osc(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(osc, "send", lambda *a, **k: None)

    snapshot = _make_snapshot()
    try:
        with pytest.raises(RoutingApplyError):
            apply_routing(osc, diagnostics, [1], snapshot, app_state)
    finally:
        osc.close()


def test_bypass_channel_toggles_insert(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()
    try:
        apply_routing(osc, diagnostics, [1], snapshot, app_state)
        assert app_state.channels[1].inserted is True

        now_inserted = bypass_channel(osc, diagnostics, 1, snapshot, app_state)
        assert now_inserted is False
        assert fake_x32.extra_responses[addresses.channel_insert_on_addr(1)] == (addresses.INSERT_OFF,)

        now_inserted = bypass_channel(osc, diagnostics, 1, snapshot, app_state)
        assert now_inserted is True
        assert fake_x32.extra_responses[addresses.channel_insert_on_addr(1)] == (addresses.INSERT_ON,)
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
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = RoutingSnapshot(
        schema_version=4,
        created_at="2026-01-01T00:00:00.000Z",
        name="restore_test",
        console={"model": "X32"},
        userrout_in=list(range(addresses.NUM_USERROUT_IN)),
        userrout_out=list(range(addresses.NUM_USERROUT_OUT)),
        routing={"routswitch": [0], "card": [0, 1, 2, 3]},
        aux_out_src=[0, 0, 0, 0, 0, 0],
        channel_inserts=[{"on": 0, "pos": 1, "sel": 0}] + [None] * 31,
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
    assert fake_x32.extra_responses[addresses.aux_out_src_addr(1)] == (0,)
    # Channel 1's captured insert (off) is replayed.
    assert fake_x32.extra_responses[addresses.channel_insert_on_addr(1)] == (0,)
    assert app_state.channels[1].inserted is False
    assert app_state.channels[1].card_out_slot is None


def test_restore_forces_off_inserted_channel_without_captured_insert(fake_x32, diagnostics, app_state):
    """A channel the app inserted but whose insert wasn't captured in the
    snapshot still gets switched off on restore (gig-safe)."""
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = RoutingSnapshot(
        schema_version=4,
        created_at="2026-01-01T00:00:00.000Z",
        name="restore_test",
        console={"model": "X32"},
        userrout_in=[None] * addresses.NUM_USERROUT_IN,
        userrout_out=[None] * addresses.NUM_USERROUT_OUT,
        routing={},
        aux_out_src=[],
        channel_inserts=[],
    )
    app_state.channels[3].inserted = True
    app_state.channels[3].card_out_slot = 1
    try:
        restore_snapshot(osc, diagnostics, snapshot, app_state)
    finally:
        osc.close()

    assert fake_x32.extra_responses[addresses.channel_insert_on_addr(3)] == (addresses.INSERT_OFF,)
