"""Channel scribble-strip (name/color) writes.

Used to show per-channel app state on the console itself (inserted vs
bypassed, AI active/suppressing) per CLAUDE.md's "Console feedback"
design. Confirmed address + argument format from a real console scene
file (2026-07-06):

    /ch/01/config "Ruby Vocal" 51 YE 1

i.e. args are (name: str, icon: int, color: token str, source_number: int)
-- one atomic node covering all four fields, not four separate addresses.

This is always read-modify-write: query the channel's current config,
change only what's being asked for (name and/or color), and write all
four fields back so icon/source_number -- which this app has no business
touching -- are preserved exactly. The "before" values are captured in the
resulting diagnostics state_change event, which is this module's snapshot
trail (per CLAUDE.md's "snapshot before touching anything").
"""
from __future__ import annotations

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection

# Color tokens directly observed in a real scene file dump. The X32's
# standard channel-strip palette is documented elsewhere as
# OFF/RD/GN/YE/BL/MG/CY/WH -- only these five are independently confirmed
# here; treat the rest as likely-correct-by-convention, not verified.
CONFIRMED_COLORS = ("RD", "GN", "YE", "BL", "CY")


def channel_config_addr(channel: int) -> str:
    return f"/ch/{channel:02d}/config"


def read_channel_config(osc: OscConnection, channel: int, correlation_id: str | None = None) -> tuple:
    """Raw (name, icon, color, source_number) tuple currently on the console."""
    return osc.query(channel_config_addr(channel), correlation_id=correlation_id)


def read_all_channel_configs(
    osc: OscConnection,
    correlation_id: str | None = None,
) -> dict[int, tuple | None]:
    """(name, icon, color, source_number) for all 32 channels in one paced
    batch (app.osc.connection.OscConnection.query_many), for the routing
    grid's name/color columns. A channel whose query timed out maps to
    None rather than raising -- one unresponsive channel shouldn't block
    displaying the other 31."""
    addr_to_channel = {channel_config_addr(ch): ch for ch in range(1, 33)}
    results = osc.query_many(list(addr_to_channel), correlation_id=correlation_id)
    return {addr_to_channel[addr]: value for addr, value in results.items()}


def write_channel_scribble(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    name: str | None = None,
    color: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Change only name and/or color; icon and source_number are read back
    and carried over unchanged."""
    current_name, icon, current_color, source_number = read_channel_config(
        osc, channel, correlation_id=correlation_id
    )
    new_name = name if name is not None else current_name
    new_color = color if color is not None else current_color

    osc.send(channel_config_addr(channel), new_name, icon, new_color, source_number, correlation_id=correlation_id)
    diagnostics.log_state_change(
        "scribble_strip_updated",
        before={"name": current_name, "color": current_color},
        after={"name": new_name, "color": new_color},
        correlation_id=correlation_id,
    )


def restore_channel_scribble(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    snapshot_config: tuple,
    correlation_id: str | None = None,
) -> None:
    """Write back a full (name, icon, color, source_number) tuple exactly as
    previously captured (e.g. from read_channel_config before this app
    first touched the channel)."""
    name, icon, color, source_number = snapshot_config
    osc.send(channel_config_addr(channel), name, icon, color, source_number, correlation_id=correlation_id)
    diagnostics.log_state_change(
        "scribble_strip_restored", after={"channel": channel, "name": name, "color": color}, correlation_id=correlation_id
    )
