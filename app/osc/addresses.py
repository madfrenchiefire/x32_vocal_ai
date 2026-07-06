"""X32 OSC address constants.

Two confidence tiers, per CLAUDE.md's "Open items to verify (do not
assume)":

CONFIRMED
    Either standard, widely-relied-upon X32 OSC addresses (``/xinfo``,
    ``/xremote``) or addresses whose exact string CLAUDE.md itself already
    specifies (``/config/userrout/in/NN`` and ``/config/userrout/out/NN``,
    channels 1-32).

UNVERIFIED (``TODO-VERIFY``)
    Best-effort placeholders for the block-level routing nodes
    (``/config/routing/IN/*`` and the CARD output blocks). CLAUDE.md
    describes these conceptually but does not give the byte-exact address
    strings, and flags them explicitly as "do not assume." These are
    read-only queries (no risk to the console either way), but the values
    returned under these addresses must be treated as unverified until
    checked against the Patrick-Gilles Maillot unofficial X32 OSC document
    or confirmed empirically against a real console. Do not use them to
    drive write/apply logic without that confirmation.
"""
from __future__ import annotations

XINFO = "/xinfo"
XREMOTE = "/xremote"

NUM_CHANNELS = 32


def userrout_in(channel: int) -> str:
    """CONFIRMED -- address string given directly in CLAUDE.md."""
    _check_channel(channel)
    return f"/config/userrout/in/{channel:02d}"


def userrout_out(channel: int) -> str:
    """CONFIRMED -- address string given directly in CLAUDE.md."""
    _check_channel(channel)
    return f"/config/userrout/out/{channel:02d}"


def all_userrout_in() -> list[str]:
    return [userrout_in(ch) for ch in range(1, NUM_CHANNELS + 1)]


def all_userrout_out() -> list[str]:
    return [userrout_out(ch) for ch in range(1, NUM_CHANNELS + 1)]


def _check_channel(channel: int) -> None:
    if not 1 <= channel <= NUM_CHANNELS:
        raise ValueError(f"channel must be 1-{NUM_CHANNELS}, got {channel}")


# --- UNVERIFIED: block-level routing (see module docstring) ---------------

ROUTING_ADDRESSES_VERIFIED = False

# Best-effort guess: 4 blocks of 8 channels, mirroring the "Config > Routing"
# grid layout described in CLAUDE.md. TODO-VERIFY against the Maillot doc or
# empirically before relying on the decoded meaning of these values -- raw
# values are still captured/stored even if these addresses turn out wrong,
# they'll just come back empty/timed-out.
ROUTING_IN_BLOCKS_TODO_VERIFY = [
    "/config/routing/IN/1-8",
    "/config/routing/IN/9-16",
    "/config/routing/IN/17-24",
    "/config/routing/IN/25-32",
]

CARD_OUT_BLOCKS_TODO_VERIFY = [
    "/config/routing/OUT/CARD/1-8",
    "/config/routing/OUT/CARD/9-16",
    "/config/routing/OUT/CARD/17-24",
    "/config/routing/OUT/CARD/25-32",
]
