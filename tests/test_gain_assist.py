from __future__ import annotations

import time

import pytest

from app.config import AppConfig
from app.osc.connection import OscConnection
from app.osc.gain_assist import (
    GainAssist,
    gain_db_to_norm,
    gain_norm_to_db,
    ha_index_addr,
    headamp_for_userrout_value,
    headamp_gain_addr,
)
from app.osc.routing_snapshot import RoutingSnapshot
from app.osc import addresses


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


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_gain_conversions_roundtrip():
    assert gain_db_to_norm(-12.0) == pytest.approx(0.0)
    assert gain_db_to_norm(60.0) == pytest.approx(1.0)
    assert gain_norm_to_db(gain_db_to_norm(24.0)) == pytest.approx(24.0)


def test_headamp_for_userrout_value_families():
    assert headamp_for_userrout_value(1) == 0  # Local Analog 1
    assert headamp_for_userrout_value(32) == 31
    assert headamp_for_userrout_value(33) == 32  # AES50-A 1
    assert headamp_for_userrout_value(81) == 80  # AES50-B 1
    assert headamp_for_userrout_value(129) is None  # Card -- no preamp
    assert headamp_for_userrout_value(0) is None
    assert headamp_for_userrout_value(None) is None


def _assist(fake_x32, diagnostics, app_state, **config_overrides) -> tuple[GainAssist, OscConnection]:
    config = AppConfig(gain_assist_enabled=True, gain_assist_cooldown_sec=0.0, **config_overrides)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    assist = GainAssist(osc=osc, diagnostics=diagnostics, config=config, state=app_state)
    assist.start()
    return assist, osc


def test_trim_steps_gain_down_via_live_ha_mapping(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[ha_index_addr(3)] = (2,)  # channel 3 fed by headamp 2
    fake_x32.extra_responses[headamp_gain_addr(2)] = (gain_db_to_norm(30.0),)

    assist, osc = _assist(fake_x32, diagnostics, app_state)
    try:
        assist.request_trim(3)
        assert _wait_for(lambda: assist.status()["trims_db"].get("2") == 2.0)
        (new_norm,) = fake_x32.extra_responses[headamp_gain_addr(2)]
        assert gain_norm_to_db(new_norm) == pytest.approx(28.0)
    finally:
        assist.stop()
        osc.close()


def test_trim_caps_at_max_total(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[ha_index_addr(1)] = (0,)
    fake_x32.extra_responses[headamp_gain_addr(0)] = (gain_db_to_norm(30.0),)

    assist, osc = _assist(fake_x32, diagnostics, app_state, gain_assist_max_total_db=4.0)
    try:
        for _ in range(5):  # only 2 x 2dB fit under the 4dB cap
            assist.request_trim(1)
            _wait_for(lambda: True, timeout=0.1)
        assert _wait_for(lambda: assist.status()["trims_db"].get("0") == 4.0)
        time.sleep(0.3)  # any further trims would land by now
        (norm,) = fake_x32.extra_responses[headamp_gain_addr(0)]
        assert gain_norm_to_db(norm) == pytest.approx(26.0)  # 30 - 4, not less
    finally:
        assist.stop()
        osc.close()


def test_restore_all_puts_original_gain_back(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[ha_index_addr(1)] = (0,)
    original = gain_db_to_norm(30.0)
    fake_x32.extra_responses[headamp_gain_addr(0)] = (original,)

    assist, osc = _assist(fake_x32, diagnostics, app_state)
    try:
        assist.request_trim(1)
        assert _wait_for(lambda: assist.status()["trims_db"].get("0") == 2.0)

        restored = assist.restore_all()
        assert restored == [0]
        # OSC floats travel as 32-bit -- compare with tolerance, not equality.
        assert _wait_for(
            lambda: abs(fake_x32.extra_responses[headamp_gain_addr(0)][0] - original) < 1e-6
        )
        assert assist.status()["trims_db"] == {}
    finally:
        assist.stop()
        osc.close()


def test_disabled_assist_ignores_requests(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[ha_index_addr(1)] = (0,)
    fake_x32.extra_responses[headamp_gain_addr(0)] = (gain_db_to_norm(30.0),)

    config = AppConfig(gain_assist_enabled=False)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    assist = GainAssist(osc=osc, diagnostics=diagnostics, config=config, state=app_state)
    assist.start()
    try:
        assist.request_trim(1)
        time.sleep(0.3)
        assert assist.status()["trims_db"] == {}
        (norm,) = fake_x32.extra_responses[headamp_gain_addr(0)]
        assert gain_norm_to_db(norm) == pytest.approx(30.0)  # untouched
    finally:
        assist.stop()
        osc.close()


def test_snapshot_fallback_when_live_mapping_says_card(fake_x32, diagnostics, app_state):
    # /-ha reports -1 (channel's live source is the Card return, as it is
    # for every inserted channel) -- the pre-app IN-block snapshot says the
    # block was AN9-16, so channel 9's original preamp is headamp 8.
    fake_x32.extra_responses[ha_index_addr(9)] = (-1,)
    fake_x32.extra_responses[headamp_gain_addr(8)] = (gain_db_to_norm(20.0),)
    app_state.current_snapshot = RoutingSnapshot(
        schema_version=3, created_at="", name="t", console={},
        userrout_in=[0] * addresses.NUM_USERROUT_IN,
        userrout_out=[0] * addresses.NUM_USERROUT_OUT,
        routing={"in": [0, 1, 2, 3, 0]},  # block 9-16 = AN9-16 (rtgin value 1)
    )

    assist, osc = _assist(fake_x32, diagnostics, app_state)
    try:
        assist.request_trim(9)
        assert _wait_for(lambda: assist.status()["trims_db"].get("8") == 2.0)
        (norm,) = fake_x32.extra_responses[headamp_gain_addr(8)]
        assert gain_norm_to_db(norm) == pytest.approx(18.0)
    finally:
        assist.stop()
        osc.close()
