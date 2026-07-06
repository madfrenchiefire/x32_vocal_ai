"""X32 OSC address constants.

Primary source: Patrick-Gilles Maillot's own reverse-engineered parameter
table and enum tables, from https://github.com/pmaillot/X32-Behringer
(X32CfgMain.h parameter table, X32.c enum string tables), inspected
2026-07-06. This is the reference implementation behind the "unofficial
X32 OSC Protocol" document CLAUDE.md points to elsewhere -- about as
authoritative as it gets short of live confirmation against real hardware.

Two address forms exist for both userrout and the routing blocks:

INDIVIDUAL (confirmed ``F_XET`` = ``F_GET | F_SET`` -- explicitly get *and*
set capable). This is what this module and app.osc.routing_snapshot query
by default:
    ``/config/userrout/in/01``..``/32``, ``/config/userrout/out/01``..``/48`` (I32)
    ``/config/routing/{IN,AES50A,AES50B,CARD,OUT,PLAY}/<block>`` (E32 enum)

BULK (``F_FND`` = "node data header" in Maillot's parser; confirmed to
appear as a single line in .scn scene file dumps; NOT confirmed whether a
live bare OSC query to the parent node replies with the full array, since
F_FND's documented role is table-walking bookkeeping in Maillot's own C
code, not a stated wire behavior):
    ``/config/userrout/in``, ``/config/userrout/out``
    ``/config/routing``, ``/config/routing/IN``, ``/AES50A``, ``/AES50B``,
    ``/CARD``, ``/OUT``, ``/PLAY``
    Kept here for reference and used by app.osc.routing_snapshot only as an
    opportunistic fallback if an individual query times out.

Enum value tables (``XCFrsw``, ``XRtgin``, ``XRtaea``, ``XRtina``,
``XRout1``, ``XRout5`` in X32.c) are reproduced in ``ROUTING_ENUM_TABLES``
so callers can decode a raw integer into its display token (e.g. index 16
in the "rtaea" table is ``"CARD1-8"``). A ``RoutingSnapshot`` always stores
the raw integer; decoding is opt-in via ``decode_routing_value()``, never
silently substituted for the raw value.
"""
from __future__ import annotations

XINFO = "/xinfo"
XREMOTE = "/xremote"

NUM_USERROUT_IN = 32
NUM_USERROUT_OUT = 48

# --- userrout: individual (confirmed F_XET), primary query targets -------


def userrout_in_addr(channel: int) -> str:
    if not 1 <= channel <= NUM_USERROUT_IN:
        raise ValueError(f"channel must be 1-{NUM_USERROUT_IN}, got {channel}")
    return f"/config/userrout/in/{channel:02d}"


def userrout_out_addr(channel: int) -> str:
    if not 1 <= channel <= NUM_USERROUT_OUT:
        raise ValueError(f"channel must be 1-{NUM_USERROUT_OUT}, got {channel}")
    return f"/config/userrout/out/{channel:02d}"


ALL_USERROUT_IN = [userrout_in_addr(ch) for ch in range(1, NUM_USERROUT_IN + 1)]
ALL_USERROUT_OUT = [userrout_out_addr(ch) for ch in range(1, NUM_USERROUT_OUT + 1)]

# --- userrout value semantics: confirmed for all four source families -----
#
# Real-hardware tests, 2026-07-06, same console (firmware 4.13), channels
# 1-8 set to "User In" (rtgin index 20) then individually assigned via the
# console's User In screen:
#   round 1: channels 1-8 -> Card 1-8 (1:1)      => userrout/in read 129-136
#   round 2: channel 1 -> Local Analog In 1      => userrout/in[0] read 1
#            channel 2 -> AES50-A In 2           => userrout/in[1] read 34
#            channels 3-8 unchanged (still Card) => userrout/in[2:8] read 131-136
#   round 3: channel 3 -> AES50-B In 3           => userrout/in[2] read 83
#            channel 4 -> AES50-B In 4           => userrout/in[3] read 84
# All four source families match a single flat, 1-indexed enumeration:
#   value = range_start + (channel_number - 1)
# with ranges in the same source order already confirmed in the
# block-routing enum tables (AN, then A/AES50-A, then B/AES50-B, then
# CARD).
USERROUT_SOURCE_RANGES: list[tuple[int, int, str]] = [
    (1, 32, "Local Analog"),
    (33, 80, "AES50-A"),
    (81, 128, "AES50-B"),
    (129, 160, "Card"),
]


def decode_userrout_value(value: int | None) -> str | None:
    """Decode a raw userrout/in or userrout/out integer into a
    "<source> <channel>" string, e.g. 34 -> "AES50-A 2". Confirmed against
    real hardware for all four source families (see comment above).
    Returns None if value is None. Every channel/console seen so far
    reports 0 for "not yet assigned via User Routing" -- not confirmed to
    mean anything more specific than that (e.g. distinct from an explicit
    "off"), so it's labeled accordingly rather than silently mapped to a
    source. Anything else outside the known ranges is reported as
    unknown, not guessed."""
    if value is None:
        return None
    if value == 0:
        return "UNSET(0)"
    for start, end, label in USERROUT_SOURCE_RANGES:
        if start <= value <= end:
            return f"{label} {value - start + 1}"
    return f"UNKNOWN({value})"

# --- userrout: bulk (scene-dump form, untested for live bare-query reply) -

USERROUT_IN = "/config/userrout/in"
USERROUT_OUT = "/config/userrout/out"

ROUTING_ADDRESSES_VERIFIED = True

# --- routing: individual per-block addresses (confirmed F_XET) -----------

ROUTING_ROUTSWITCH = "/config/routing/routswitch"

ROUTING_IN_BLOCKS = [
    "/config/routing/IN/1-8",
    "/config/routing/IN/9-16",
    "/config/routing/IN/17-24",
    "/config/routing/IN/25-32",
]
ROUTING_IN_AUX = "/config/routing/IN/AUX"

ROUTING_AES50A_BLOCKS = [
    "/config/routing/AES50A/1-8",
    "/config/routing/AES50A/9-16",
    "/config/routing/AES50A/17-24",
    "/config/routing/AES50A/25-32",
    "/config/routing/AES50A/33-40",
    "/config/routing/AES50A/41-48",
]
ROUTING_AES50B_BLOCKS = [
    "/config/routing/AES50B/1-8",
    "/config/routing/AES50B/9-16",
    "/config/routing/AES50B/17-24",
    "/config/routing/AES50B/25-32",
    "/config/routing/AES50B/33-40",
    "/config/routing/AES50B/41-48",
]
ROUTING_CARD_BLOCKS = [
    "/config/routing/CARD/1-8",
    "/config/routing/CARD/9-16",
    "/config/routing/CARD/17-24",
    "/config/routing/CARD/25-32",
]
# Sequential channel order (1-4, 5-8, 9-12, 13-16); note Maillot's own table
# declares these out of order (1-4, 9-12, 5-8, 13-16) -- order here is by
# channel range, not declaration order.
ROUTING_OUT_BLOCKS = [
    "/config/routing/OUT/1-4",
    "/config/routing/OUT/5-8",
    "/config/routing/OUT/9-12",
    "/config/routing/OUT/13-16",
]
ROUTING_PLAY_BLOCKS = [
    "/config/routing/PLAY/1-8",
    "/config/routing/PLAY/9-16",
    "/config/routing/PLAY/17-24",
    "/config/routing/PLAY/25-32",
]
ROUTING_PLAY_AUX = "/config/routing/PLAY/AUX"

# --- routing: bulk (scene-dump form, untested for live bare-query reply) -

ROUTING_REC = "/config/routing"
ROUTING_IN = "/config/routing/IN"
ROUTING_AES50A = "/config/routing/AES50A"
ROUTING_AES50B = "/config/routing/AES50B"
ROUTING_CARD = "/config/routing/CARD"
ROUTING_OUT = "/config/routing/OUT"
ROUTING_PLAY = "/config/routing/PLAY"

# --- enum value tables (X32.c, verbatim strings minus the leading space) -

# Note on the trailing "User" entry in each list below: Maillot's X32.c
# source terminates each of these string arrays with an empty "" (e.g.
# XRtgin[] = {..., "CARD25-32", ""}), which reads like an end-of-array
# sentinel. It isn't -- CONFIRMED 2026-07-06 against real hardware: setting
# a block's source to "User In"/"User Out" on the console changed its raw
# value from a physical-source index to exactly one past the last named
# entry (e.g. /config/routing/IN/1-8 went from 0 ("AN1-8") to 20, one past
# rtgin's 20 named entries). Added here as "USER" for rtgin (directly
# confirmed) and, by the same pattern, for rtaea/rtina/rout1/rout5 (not yet
# independently confirmed on real hardware -- do the same test on an
# AES50A/AES50B/OUT/AUX block to verify before relying on it).
ROUTING_ENUM_TABLES: dict[str, list[str]] = {
    "routswitch": ["REC", "PLAY"],
    "rtgin": [
        "AN1-8", "AN9-16", "AN17-24", "AN25-32", "A1-8", "A9-16", "A17-24", "A25-32",
        "A33-40", "A41-48", "B1-8", "B9-16", "B17-24", "B25-32", "B33-40", "B41-48",
        "CARD1-8", "CARD9-16", "CARD17-24", "CARD25-32",
        "USER",  # confirmed: index 20, see note above
    ],
    "rtaea": [
        "AN1-8", "AN9-16", "AN17-24", "AN25-32", "A1-8", "A9-16", "A17-24", "A25-32",
        "A33-40", "A41-48", "B1-8", "B9-16", "B17-24", "B25-32", "B33-40", "B41-48",
        "CARD1-8", "CARD9-16", "CARD17-24", "CARD25-32", "OUT1-8", "OUT9-16",
        "P161-8", "P169-16", "AUX1-6/Mon", "AuxIN1-6/TB",
        "USER",  # inferred by pattern, not yet independently confirmed
    ],
    "rtina": [
        "AUX1-4", "AN1-2", "AN1-4", "AN1-6", "A1-2", "A1-4", "A1-6",
        "B1-2", "B1-4", "B1-6", "CARD1-2", "CARD1-4", "CARD1-6",
        "USER",  # inferred by pattern, not yet independently confirmed
    ],
    "rout1": [
        "AN1-4", "AN9-12", "AN17-20", "AN25-28", "A1-4", "A9-12", "A17-20", "A25-28",
        "A33-36", "A41-44", "B1-4", "B9-12", "B17-20", "B25-28", "B33-36", "B41-44",
        "CARD1-4", "CARD9-12", "CARD17-20", "CARD25-28", "OUT1-4", "OUT9-12",
        "P161-4", "P169-12", "AUX/CR", "AUX/TB",
        "USER",  # inferred by pattern, not yet independently confirmed
    ],
    "rout5": [
        "AN5-8", "AN13-16", "AN21-24", "AN29-32", "A5-8", "A13-16", "A21-24", "A29-32",
        "A37-40", "A45-48", "B5-8", "B13-16", "B21-24", "B29-32", "B37-40", "B45-48",
        "CARD5-8", "CARD13-16", "CARD21-24", "CARD29-32", "OUT5-8", "OUT13-16",
        "P165-8", "P1613-16", "AUX/CR", "AUX/TB",
        "USER",  # inferred by pattern, not yet independently confirmed
    ],
}


def decode_routing_value(table: str, value: int | None) -> str | None:
    """Decode a raw enum int into its display token, e.g.
    ``decode_routing_value("rtaea", 16) == "CARD1-8"``. Returns None if
    value is None, or ``f"UNKNOWN({value})"`` if out of range for the
    known table (e.g. a firmware revision with more options than
    Maillot's tables enumerate)."""
    if value is None:
        return None
    tokens = ROUTING_ENUM_TABLES[table]
    if 0 <= value < len(tokens):
        return tokens[value]
    return f"UNKNOWN({value})"


# Named groups in snapshot report order: each entry is (address, enum table
# name) so a raw reply can be decoded with decode_routing_value(table, v).
ROUTING_GROUPS: dict[str, list[tuple[str, str]]] = {
    "routswitch": [(ROUTING_ROUTSWITCH, "routswitch")],
    "in": [(a, "rtgin") for a in ROUTING_IN_BLOCKS] + [(ROUTING_IN_AUX, "rtina")],
    "aes50a": [(a, "rtaea") for a in ROUTING_AES50A_BLOCKS],
    "aes50b": [(a, "rtaea") for a in ROUTING_AES50B_BLOCKS],
    "card": [(a, "rtaea") for a in ROUTING_CARD_BLOCKS],
    "out": [
        (ROUTING_OUT_BLOCKS[0], "rout1"),  # 1-4
        (ROUTING_OUT_BLOCKS[1], "rout5"),  # 5-8
        (ROUTING_OUT_BLOCKS[2], "rout1"),  # 9-12
        (ROUTING_OUT_BLOCKS[3], "rout5"),  # 13-16
    ],
    "play": [(a, "rtgin") for a in ROUTING_PLAY_BLOCKS] + [(ROUTING_PLAY_AUX, "rtina")],
}

# Bulk fallback address per group -- used by app.osc.routing_snapshot only
# when one or more individual queries in the group time out.
ROUTING_GROUP_BULK_ADDR: dict[str, str] = {
    "routswitch": ROUTING_REC,
    "in": ROUTING_IN,
    "aes50a": ROUTING_AES50A,
    "aes50b": ROUTING_AES50B,
    "card": ROUTING_CARD,
    "out": ROUTING_OUT,
    "play": ROUTING_PLAY,
}
