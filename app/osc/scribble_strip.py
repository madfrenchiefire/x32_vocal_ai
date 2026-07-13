"""Channel scribble-strip (name/color) reads and writes.

Used to show per-channel app state on the console itself (inserted vs
bypassed, AI active/suppressing) per CLAUDE.md's "Console feedback"
design, and to pull channel names/colors into the web UI's routing grid.

**Use the leaf addresses, not the parent node -- confirmed on real
hardware (2026-07-07, firmware 4.13).** A scene-file dump serializes one
atomic parent node per channel (`/ch/01/config "Ruby Vocal" 51 YE 1`), but
a live bare OSC query on `/ch/NN/config` gets NO reply -- two passive
captures against a real console returned nothing for all 32 channels.
This is the same parent-vs-leaf pattern already confirmed for the routing
tree (bulk nodes appear in scene dumps but don't answer live queries).
The console *pushes* leaf addresses when a channel is renamed on the desk
(`/ch/02/config/name 'Test'` caught by the /xremote sniffer), and leaves
are individually get+set-able:

    /ch/NN/config/name   -- string
    /ch/NN/config/color  -- int enum (see SCRIBBLE_COLORS below)

Working at the leaf level also means icon/source_number -- which this app
has no business touching -- are simply never written, rather than needing
the previous read-modify-write dance around the (unqueryable) 4-field
parent node.
"""
from __future__ import annotations

from typing import Any

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection

NUM_CHANNELS = 32

# The X32's standard scribble palette, indexed by the raw
# /ch/NN/config/color int -- doc-confirmed (X32_OSC.pdf, committed in this
# repo: "/ch/[01-32]/config/color: enum int [0-15] representing {OFF, RD,
# GN, YE, BL, MG, CY, WH, OFFi, RDi, GNi, YEi, BLi, MGi, CYi, WHi}").
# The tokens RD/GN/YE/BL/CY additionally appear verbatim in a real
# scene-file dump.
SCRIBBLE_COLORS = (
    "OFF", "RD", "GN", "YE", "BL", "MG", "CY", "WH",
    "OFFi", "RDi", "GNi", "YEi", "BLi", "MGi", "CYi", "WHi",
)


def channel_name_addr(channel: int) -> str:
    return f"/ch/{channel:02d}/config/name"


def channel_color_addr(channel: int) -> str:
    return f"/ch/{channel:02d}/config/color"


def decode_color(raw: Any) -> str | None:
    """Raw /ch/NN/config/color reply -> palette token ('YE', 'BLi', ...).
    Tolerates a console that answers with the token string directly, and
    reports out-of-table ints as UNKNOWN(n) rather than guessing."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int) and 0 <= raw < len(SCRIBBLE_COLORS):
        return SCRIBBLE_COLORS[raw]
    return f"UNKNOWN({raw})"


def read_all_channel_configs(
    osc: OscConnection,
    correlation_id: str | None = None,
) -> dict[int, tuple[str | None, str | None]]:
    """(name, color_token) for all 32 channels in one paced batch
    (app.osc.connection.OscConnection.query_many over the 64 leaf
    addresses), for the routing grid's name/color columns. A leaf whose
    query timed out maps to None for that field rather than raising --
    one unresponsive channel shouldn't block displaying the other 31."""
    name_addrs = [channel_name_addr(ch) for ch in range(1, NUM_CHANNELS + 1)]
    color_addrs = [channel_color_addr(ch) for ch in range(1, NUM_CHANNELS + 1)]
    results = osc.query_many(name_addrs + color_addrs, correlation_id=correlation_id)

    configs: dict[int, tuple[str | None, str | None]] = {}
    for ch in range(1, NUM_CHANNELS + 1):
        name_reply = results[channel_name_addr(ch)]
        color_reply = results[channel_color_addr(ch)]
        name = name_reply[0] if name_reply else None
        color = decode_color(color_reply[0]) if color_reply else None
        configs[ch] = (name, color)
    return configs


def read_channel_config(
    osc: OscConnection, channel: int, correlation_id: str | None = None
) -> tuple[str | None, Any]:
    """(name, raw_color) currently on the console, from the two leaf
    addresses. Either field is None if its query times out. The color is
    returned raw (not decoded) so it can round-trip through
    restore_channel_scribble unchanged."""
    results = osc.query_many(
        [channel_name_addr(channel), channel_color_addr(channel)], correlation_id=correlation_id
    )
    name_reply = results[channel_name_addr(channel)]
    color_reply = results[channel_color_addr(channel)]
    return (
        name_reply[0] if name_reply else None,
        color_reply[0] if color_reply else None,
    )


def _color_to_raw(color: str | int) -> int | str:
    """Palette token -> raw int for writing; ints (already raw) and
    unrecognized strings pass through untouched."""
    if isinstance(color, str) and color in SCRIBBLE_COLORS:
        return SCRIBBLE_COLORS.index(color)
    return color


def write_channel_scribble(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    name: str | None = None,
    color: str | int | None = None,
    correlation_id: str | None = None,
) -> None:
    """Write name and/or color via their leaf addresses. Fields not given
    are simply not written (no read-modify-write needed at leaf level)."""
    before_name, before_color = read_channel_config(osc, channel, correlation_id=correlation_id)
    if name is not None:
        osc.send(channel_name_addr(channel), name, correlation_id=correlation_id)
    if color is not None:
        osc.send(channel_color_addr(channel), _color_to_raw(color), correlation_id=correlation_id)
    diagnostics.log_state_change(
        "scribble_strip_updated",
        before={"name": before_name, "color": before_color},
        after={"name": name, "color": color},
        correlation_id=correlation_id,
    )


def restore_channel_scribble(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    snapshot_config: tuple,
    correlation_id: str | None = None,
) -> None:
    """Write back a (name, raw_color) pair exactly as previously captured
    by read_channel_config. A None field (its read timed out at snapshot
    time) is left alone rather than guessed at."""
    name, color = snapshot_config
    if name is not None:
        osc.send(channel_name_addr(channel), name, correlation_id=correlation_id)
    if color is not None:
        osc.send(channel_color_addr(channel), color, correlation_id=correlation_id)
    diagnostics.log_state_change(
        "scribble_strip_restored", after={"channel": channel, "name": name, "color": color}, correlation_id=correlation_id
    )
