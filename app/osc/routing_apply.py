"""Routing apply/restore -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's routing-automation steps 2-4 (writing userrout/in and
userrout/out entries in blocks-of-8-safe fashion, flipping block routing to
User In/Out last, pacing writes, reading back to confirm) plus per-channel
bypass/restore and full-snapshot restore. Depends on the block-level
routing addresses being verified (see app.osc.addresses) before any write
logic can be trusted -- do not implement against the TODO-VERIFY
placeholders.
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
