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

# --- userrout value semantics ---------------------------------------------
#
# The four physical-source ranges were first confirmed against real
# hardware (2026-07-06, firmware 4.13: Local Analog In 1 read 1, AES50-A
# In 2 read 34, AES50-B In 3/4 read 83/84, Card 1-8 read 129-136), then
# the FULL table below was taken from Maillot's "Unofficial X32/M32 OSC
# Remote Protocol" doc v4.09 (committed in this repo as X32_OSC.pdf,
# /config/userrout/out section) -- which exactly matches every empirical
# value, including 0 = OFF (previously decoded as "UNSET(0)" pending
# confirmation) and 183/184 (read off a real console with Main L/R patched
# through Outputs 15/16 -- the doc shows those values mean "Output 15/16",
# i.e. the userrout taps the *physical output* signal, and it carried
# Main L/R only because the Out 1-16 tab patched it that way; see
# app.audio.echo_cancellation for why that distinction is load-bearing).
#
# userrout/out accepts the whole table (0-208); userrout/in only 0-168
# (everything through TB External).
USERROUT_SOURCE_RANGES: list[tuple[int, int, str]] = [
    (1, 32, "Local Analog"),
    (33, 80, "AES50-A"),
    (81, 128, "AES50-B"),
    (129, 160, "Card"),
    (161, 166, "Aux In"),
    (169, 184, "Output"),
    (185, 200, "P16"),
    (201, 206, "Aux Out"),
]

USERROUT_NAMED_VALUES: dict[int, str] = {
    0: "OFF",
    167: "TB Internal",
    168: "TB External",
    207: "Monitor L",
    208: "Monitor R",
}

# userrout/out value for physical Output N (1-16): 169 + (N - 1).
OUTPUT_USERROUT_OUT_BASE = 169


def output_userrout_out_value(output_number: int) -> int:
    """userrout/out raw value that taps physical Output N (1-16)."""
    if not 1 <= output_number <= 16:
        raise ValueError(f"output_number must be 1-16, got {output_number}")
    return OUTPUT_USERROUT_OUT_BASE + output_number - 1


def decode_userrout_value(value: int | None) -> str | None:
    """Decode a raw userrout/in or userrout/out integer into a
    "<source> <channel>" string, e.g. 34 -> "AES50-A 2", 183 ->
    "Output 15", 0 -> "OFF". Table above (empirically confirmed ranges +
    Maillot doc for the rest). Returns None if value is None; anything
    outside the table is reported as unknown, not guessed."""
    if value is None:
        return None
    if value in USERROUT_NAMED_VALUES:
        return USERROUT_NAMED_VALUES[value]
    for start, end, label in USERROUT_SOURCE_RANGES:
        if start <= value <= end:
            return f"{label} {value - start + 1}"
    return f"UNKNOWN({value})"

# --- physical output patch (/outputs/main, from X32_OSC.pdf) ---------------
#
# The Out 1-16 tab: each physical output's source and tap point.
# /outputs/main/NN/src is int 0-76: {OFF, Main L, Main R, M/C,
# MixBus 01-16, Matrix 1-6, DirectOut Ch 01-32, DirectOut Aux 1-8,
# DirectOut FX 1L-4R, Monitor L, Monitor R, Talkback};
# /outputs/main/NN/pos is int 0-8: {IN/LC, IN/LC+M, <-EQ, <-EQ+M, EQ->,
# EQ->+M, PRE, PRE+M, POST}.
NUM_MAIN_OUTPUTS = 16
OUTPUT_SRC_MAIN_L = 1
OUTPUT_SRC_MAIN_R = 2
OUTPUT_POS_POST_FADER = 8


def output_src_addr(output_number: int) -> str:
    if not 1 <= output_number <= NUM_MAIN_OUTPUTS:
        raise ValueError(f"output_number must be 1-{NUM_MAIN_OUTPUTS}, got {output_number}")
    return f"/outputs/main/{output_number:02d}/src"


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

# Note on the trailing "User" entries in each list below: Maillot's X32.c
# source terminates each of these string arrays with a single empty "" (e.g.
# XRtgin[] = {..., "CARD25-32", ""}), which reads like a single end-of-array
# sentinel/generic "User" option. It isn't just one option, and it isn't
# even always the *same* User pool:
#
# There are two independent 8-channel-banked "User Routing" pools on the
# console -- User In (32 slots, /config/userrout/in/NN) and User Out (48
# slots, /config/userrout/out/NN) -- and which one a given "User" enum
# value pulls from depends on the *direction* of the block it's on, not
# just its table:
#   - rtgin (IN and PLAY blocks -- feeding INTO the channel strips / local
#     outs): confirmed 2026-07-06 against real hardware, cross-checked with
#     the console's Setup > Routing > Inputs matrix screen -- 20/21/22/23 =
#     "User In 1-8"/"9-16"/"17-24"/"25-32", one past rtgin's 20 named
#     physical sources, all four directly confirmed by writing each value
#     and watching the orange selector move to the matching column.
#   - rtaea (AES50-A/AES50-B/CARD blocks -- feeding a *send*, i.e. what
#     goes out to the AES50 network or gets recorded via the Card/USB
#     interface): confirmed 2026-07-06 -- /config/routing/CARD/9-16 set to
#     26 (one past rtaea's 26 named sources) showed as "User Out 1-8" on
#     the console's routing matrix, not "User In 1-8". This matches
#     CLAUDE.md's own routing-automation design ("use userrout/out + CARD
#     block routing to cherry-pick arbitrary channels onto Card outs") --
#     CARD (and by the same table, AES50-A/AES50-B) pull from the 48-slot
#     User *Out* pool, banked the same way AES50-A/B's own physical blocks
#     already are (6 banks of 8: 1-8/9-16/.../41-48). Only the first bank
#     (26 = "User Out 1-8") is directly confirmed; 27-31 are inferred by
#     the same sequential pattern.
#   - rout1 (OUT, physical analog outputs -- also a "send" direction like
#     rtaea): confirmed 2026-07-06 -- /config/routing/OUT/1-4 set to 26
#     (one past rout1's 26 named sources) displayed as "User Out 1-8" on
#     the console's "XLR" routing matrix tab. Notably the OUT block itself
#     is 4-channels-wide (1-4/5-8/9-12/13-16) but still pulled from an
#     8-wide User Out bank -- User Out banking is fixed at 8 regardless of
#     the consuming block's own width. rout5 shares the same table/pool by
#     construction (it's the other half of the same 4-wide OUT blocks) but
#     hasn't been independently written to; treat its bank entries as
#     inferred, not confirmed, until tested directly the same way.
#   - rtina (IN/AUX, PLAY/AUX): confirmed 2026-07-06 -- /config/routing/IN/AUX
#     set to 13 (one past rtina's 13 named sources) matched "User In 1-2"
#     both by readback and by the console's own "Aux In Remap" dropdown,
#     which lists the full enum in order: the 13 named entries, then "User
#     In 1-2"/"1-4"/"1-6" (indices 13/14/15) -- User In as guessed by
#     direction, but banked in 2/4/6-channel groups (matching AUX's own
#     6-channel width) rather than the 8-wide banks used elsewhere.
#
# IMPORTANT for routing writes: which User bank a block pulls from must
# match the per-channel userrout slot you actually configured, or the
# console will use a different (likely unconfigured) slot for real audio --
# confirmed by testing: block IN/9-16 set to raw value 20 ("User In 1-8")
# displayed channel 9's own userrout/in/09 value correctly on the
# per-channel config screen, but the routing matrix showed channels 9-16
# actually sourced from User In bank 1-8, not 9-16, meaning slot 09's value
# would not have driven real audio for channel 9 in that state. Only after
# setting the block to 21 ("User In 9-16") did the matrix confirm channels
# 9-16 correctly draw from User In slots 9-16.
ROUTING_ENUM_TABLES: dict[str, list[str]] = {
    "routswitch": ["REC", "PLAY"],
    "rtgin": [
        "AN1-8", "AN9-16", "AN17-24", "AN25-32", "A1-8", "A9-16", "A17-24", "A25-32",
        "A33-40", "A41-48", "B1-8", "B9-16", "B17-24", "B25-32", "B33-40", "B41-48",
        "CARD1-8", "CARD9-16", "CARD17-24", "CARD25-32",
        "USERIN1-8",    # confirmed: index 20
        "USERIN9-16",   # confirmed: index 21
        "USERIN17-24",  # confirmed: index 22
        "USERIN25-32",  # confirmed: index 23
    ],
    "rtaea": [
        "AN1-8", "AN9-16", "AN17-24", "AN25-32", "A1-8", "A9-16", "A17-24", "A25-32",
        "A33-40", "A41-48", "B1-8", "B9-16", "B17-24", "B25-32", "B33-40", "B41-48",
        "CARD1-8", "CARD9-16", "CARD17-24", "CARD25-32", "OUT1-8", "OUT9-16",
        "P161-8", "P169-16", "AUX1-6/Mon", "AuxIN1-6/TB",
        "USEROUT1-8",    # confirmed: index 26
        "USEROUT9-16",   # inferred by pattern, not yet independently confirmed
        "USEROUT17-24",  # inferred by pattern, not yet independently confirmed
        "USEROUT25-32",  # inferred by pattern, not yet independently confirmed
        "USEROUT33-40",  # inferred by pattern, not yet independently confirmed
        "USEROUT41-48",  # inferred by pattern, not yet independently confirmed
    ],
    "rtina": [
        "AUX1-4", "AN1-2", "AN1-4", "AN1-6", "A1-2", "A1-4", "A1-6",
        "B1-2", "B1-4", "B1-6", "CARD1-2", "CARD1-4", "CARD1-6",
        "USERIN1-2",  # confirmed: index 13 (write + the console's own "Aux In Remap" dropdown listing)
        "USERIN1-4",  # confirmed: index 14 (seen in the same dropdown listing, in order)
        "USERIN1-6",  # confirmed: index 15 (seen in the same dropdown listing, in order)
    ],
    "rout1": [
        "AN1-4", "AN9-12", "AN17-20", "AN25-28", "A1-4", "A9-12", "A17-20", "A25-28",
        "A33-36", "A41-44", "B1-4", "B9-12", "B17-20", "B25-28", "B33-36", "B41-44",
        "CARD1-4", "CARD9-12", "CARD17-20", "CARD25-28", "OUT1-4", "OUT9-12",
        "P161-4", "P169-12", "AUX/CR", "AUX/TB",
        "USEROUT1-8",    # confirmed: index 26
        "USEROUT9-16",   # inferred by pattern, not yet independently confirmed
        "USEROUT17-24",  # inferred by pattern, not yet independently confirmed
        "USEROUT25-32",  # inferred by pattern, not yet independently confirmed
        "USEROUT33-40",  # inferred by pattern, not yet independently confirmed
        "USEROUT41-48",  # inferred by pattern, not yet independently confirmed
    ],
    "rout5": [
        "AN5-8", "AN13-16", "AN21-24", "AN29-32", "A5-8", "A13-16", "A21-24", "A29-32",
        "A37-40", "A45-48", "B5-8", "B13-16", "B21-24", "B29-32", "B37-40", "B45-48",
        "CARD5-8", "CARD13-16", "CARD21-24", "CARD29-32", "OUT5-8", "OUT13-16",
        "P165-8", "P1613-16", "AUX/CR", "AUX/TB",
        "USEROUT1-8",    # inferred by pattern (rout1's twin table) -- not yet independently confirmed
        "USEROUT9-16",   # inferred by pattern, not yet independently confirmed
        "USEROUT17-24",  # inferred by pattern, not yet independently confirmed
        "USEROUT25-32",  # inferred by pattern, not yet independently confirmed
        "USEROUT33-40",  # inferred by pattern, not yet independently confirmed
        "USEROUT41-48",  # inferred by pattern, not yet independently confirmed
    ],
}

USER_IN_BASE_VALUE = 20  # rtgin index of "User In 1-8" -- confirmed


def user_in_block_value(channel: int) -> int:
    """Raw rtgin value that sets a channel's containing 8-channel block to
    pull from the User In bank matching that channel's own position (e.g.
    channel 9 -> block covering 9-16 -> "User In 9-16" -> 21). This is the
    pairing needed for that channel's own /config/userrout/in/NN value to
    actually drive real audio -- see the note above ROUTING_ENUM_TABLES."""
    if not 1 <= channel <= NUM_USERROUT_IN:
        raise ValueError(f"channel must be 1-{NUM_USERROUT_IN}, got {channel}")
    return USER_IN_BASE_VALUE + (channel - 1) // 8


def equivalent_userrout_in_value(rtgin_block_value: int, channel: int) -> int | None:
    """For a channel whose containing block currently has a *physical*
    source (i.e. rtgin_block_value is one of the 20 named entries, not
    already a User In bank), return the userrout/in value that replicates
    that exact same physical source for this one channel.

    This is what makes flipping a block to User In safe for the *other*
    channels sharing it: e.g. if block 9-16 is on "AN9-16" (Local Analog
    9-16, a straight 1:1 mapping), channel 9 is physically getting Local
    Analog In 9 -- so before flipping that block to User In, channel 9's
    own userrout/in must be set to 9 (Local Analog 9, per
    decode_userrout_value's confirmed ranges) or it would go silent once
    the block stops using its physical source directly.

    Returns None if rtgin_block_value is already a User In value (nothing
    to replicate -- the channel's existing userrout/in is presumably
    already meaningful) or out of the known 0-19 physical range.
    """
    if not 0 <= rtgin_block_value <= 19:
        return None
    block_start = ((channel - 1) // 8) * 8 + 1
    offset = channel - block_start  # 0-7

    if 0 <= rtgin_block_value <= 3:  # Local Analog: AN1-8, AN9-16, AN17-24, AN25-32
        source_base = 1 + rtgin_block_value * 8
    elif 4 <= rtgin_block_value <= 9:  # AES50-A: A1-8 .. A41-48
        source_base = 33 + (rtgin_block_value - 4) * 8
    elif 10 <= rtgin_block_value <= 15:  # AES50-B: B1-8 .. B41-48
        source_base = 81 + (rtgin_block_value - 10) * 8
    else:  # 16-19: Card: CARD1-8 .. CARD25-32
        source_base = 129 + (rtgin_block_value - 16) * 8

    return source_base + offset


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

# Reverse lookup: individual routing address -> its decode table name, for
# tools that take an arbitrary address on the command line rather than
# already knowing which group/table it belongs to (see
# app.tools.test_write_routing).
ROUTING_ADDRESS_TABLE: dict[str, str] = {
    addr: table for pairs in ROUTING_GROUPS.values() for addr, table in pairs
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
