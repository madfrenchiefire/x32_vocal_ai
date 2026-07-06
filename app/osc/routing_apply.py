"""Routing apply/restore -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's routing-automation steps 2-4: write ``/config/userrout/in``
and ``/config/userrout/out`` (flipping block routing to User In/Out last,
pacing writes, reading back to confirm) plus per-channel bypass/restore and
full-snapshot restore.

Confirmed from Patrick-Gilles Maillot's reverse-engineered parameter table
(see app.osc.addresses): each channel has its own individually
get+set-able address (``/config/userrout/in/01``..``/32``,
``/config/userrout/out/01``..``/48``, flagged ``F_XET`` in that table).
"Rewrite one channel's userrout entry" (CLAUDE.md's per-channel bypass) is
therefore a genuinely single-value write to that channel's own address --
no read-modify-write of a larger array required. The same applies to the
block-level routing addresses (``/config/routing/IN/1-8`` etc.) used when
flipping a block to User In/Out.
"""
from __future__ import annotations

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot


def apply_routing(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    selected_channels: list[int],
    snapshot: RoutingSnapshot,
    correlation_id: str | None = None,
) -> None:
    raise NotImplementedError("routing apply is implemented in a later phase")


def bypass_channel(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    snapshot: RoutingSnapshot,
    correlation_id: str | None = None,
) -> None:
    raise NotImplementedError("per-channel bypass/restore is implemented in a later phase")


def restore_snapshot(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    snapshot: RoutingSnapshot,
    correlation_id: str | None = None,
) -> None:
    raise NotImplementedError("full snapshot restore is implemented in a later phase")
