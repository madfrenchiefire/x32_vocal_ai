from __future__ import annotations

import pytest

from app.osc import addresses


def test_decode_routing_value_known_tokens():
    assert addresses.decode_routing_value("rtaea", 16) == "CARD1-8"
    assert addresses.decode_routing_value("rtgin", 0) == "AN1-8"


def test_decode_routing_value_user_banks_confirmed_on_rtgin():
    # Confirmed 2026-07-06 against real hardware, cross-checked with the
    # console's own routing matrix screen: "User In" is split into the same
    # four 8-channel banks as every other source type. Setting a channel
    # block's source to "User In 1-8" changed /config/routing/IN/1-8 from 0
    # ("AN1-8") to 20; all four banks (1-8/9-16/17-24/25-32 -> 20/21/22/23)
    # are directly confirmed the same way.
    assert addresses.decode_routing_value("rtgin", 20) == "USERIN1-8"
    assert addresses.decode_routing_value("rtgin", 21) == "USERIN9-16"
    assert addresses.decode_routing_value("rtgin", 22) == "USERIN17-24"
    assert addresses.decode_routing_value("rtgin", 23) == "USERIN25-32"


def test_decode_routing_value_user_out_confirmed_on_rtaea():
    # Confirmed 2026-07-06 against real hardware: /config/routing/CARD/9-16
    # set to 26 (one past rtaea's 26 named physical sources) showed as
    # "User Out 1-8" on the console's routing matrix -- CARD (and by the
    # same table, AES50-A/AES50-B) pull from the 48-slot User *Out* pool,
    # not User In. Only this first bank is directly confirmed; the rest
    # are inferred by the same sequential pattern.
    assert addresses.decode_routing_value("rtaea", 26) == "USEROUT1-8"
    assert addresses.decode_routing_value("rtaea", 31) == "USEROUT41-48"


def test_user_in_block_value_matches_channel_position():
    # Each channel's containing 8-channel block must pull from the User In
    # bank matching that channel's own position, or the console uses a
    # different (likely unconfigured) slot for real audio -- see the note
    # above app.osc.addresses.ROUTING_ENUM_TABLES.
    assert addresses.user_in_block_value(1) == 20  # block 1-8 -> USERIN1-8
    assert addresses.user_in_block_value(8) == 20
    assert addresses.user_in_block_value(9) == 21  # block 9-16 -> USERIN9-16 (confirmed)
    assert addresses.user_in_block_value(16) == 21
    assert addresses.user_in_block_value(17) == 22  # block 17-24 -> USERIN17-24 (confirmed)
    assert addresses.user_in_block_value(24) == 22
    assert addresses.user_in_block_value(25) == 23  # block 25-32 -> USERIN25-32 (confirmed)
    assert addresses.user_in_block_value(32) == 23


def test_user_in_block_value_rejects_out_of_range_channel():
    with pytest.raises(ValueError):
        addresses.user_in_block_value(0)
    with pytest.raises(ValueError):
        addresses.user_in_block_value(33)


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
