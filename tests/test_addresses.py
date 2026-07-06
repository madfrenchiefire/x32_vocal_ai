from __future__ import annotations

from app.osc import addresses


def test_decode_routing_value_known_tokens():
    assert addresses.decode_routing_value("rtaea", 16) == "CARD1-8"
    assert addresses.decode_routing_value("rtgin", 0) == "AN1-8"


def test_decode_routing_value_user_confirmed_on_rtgin():
    # Confirmed 2026-07-06 against real hardware: setting a channel block's
    # source to "User In" on the console changed /config/routing/IN/1-8
    # from 0 ("AN1-8") to 20, one past rtgin's 20 named physical sources.
    assert addresses.decode_routing_value("rtgin", 20) == "USER"


def test_decode_routing_value_none_passthrough():
    assert addresses.decode_routing_value("rtgin", None) is None


def test_decode_routing_value_out_of_range_is_reported_not_silently_wrong():
    assert addresses.decode_routing_value("rtgin", 999) == "UNKNOWN(999)"
