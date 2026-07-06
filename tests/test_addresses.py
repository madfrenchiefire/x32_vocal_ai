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


def test_decode_userrout_value_confirmed_source_families():
    # Confirmed 2026-07-06 against real hardware (firmware 4.13):
    # channel -> Local Analog In 1 read back as 1.
    assert addresses.decode_userrout_value(1) == "Local Analog 1"
    # channel -> AES50-A In 2 read back as 34.
    assert addresses.decode_userrout_value(34) == "AES50-A 2"
    # channel -> Card 3 read back as 131.
    assert addresses.decode_userrout_value(131) == "Card 3"
    # Range boundaries.
    assert addresses.decode_userrout_value(32) == "Local Analog 32"
    assert addresses.decode_userrout_value(33) == "AES50-A 1"
    assert addresses.decode_userrout_value(80) == "AES50-A 48"
    assert addresses.decode_userrout_value(129) == "Card 1"
    assert addresses.decode_userrout_value(160) == "Card 32"


def test_decode_userrout_value_aes50b():
    # Confirmed 2026-07-06 against real hardware: channel -> AES50-B In 3
    # read back as 83, channel -> AES50-B In 4 read back as 84.
    assert addresses.decode_userrout_value(83) == "AES50-B 3"
    assert addresses.decode_userrout_value(84) == "AES50-B 4"
    assert addresses.decode_userrout_value(81) == "AES50-B 1"
    assert addresses.decode_userrout_value(128) == "AES50-B 48"


def test_decode_userrout_value_unset_and_unknown():
    assert addresses.decode_userrout_value(0) == "UNSET(0)"
    assert addresses.decode_userrout_value(None) is None
    assert addresses.decode_userrout_value(999) == "UNKNOWN(999)"
