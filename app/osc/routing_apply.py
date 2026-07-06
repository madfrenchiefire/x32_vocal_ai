"""Routing apply/restore -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's routing-automation steps 2-4: write ``/config/userrout/in``
and ``/config/userrout/out`` (flipping block routing to User In/Out last,
pacing writes, reading back to confirm) plus per-channel bypass/restore and
full-snapshot restore.

Confirmed from a real scene file (see app.osc.addresses): userrout/in and
userrout/out are each a *single* address carrying the whole 32-/48-element
array, not one address per channel. That means "rewrite one channel's
userrout entry" (CLAUDE.md's per-channel bypass) is: read the array from
the snapshot, mutate the one index for the target channel, and send the
whole array back as one message -- still a single write, just array-shaped.
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
