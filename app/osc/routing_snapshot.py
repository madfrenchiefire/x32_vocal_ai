"""Routing snapshot: read the console's current routing state and persist
it as a named JSON file.

Per CLAUDE.md step 2 of routing automation ("Snapshot before touching
anything"): this is the read side only. Nothing here writes to the
console. Applying/restoring a snapshot is a separate, not-yet-implemented
module (app.osc.routing_apply).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import OscConnection

SNAPSHOT_SCHEMA_VERSION = 1


@dataclass
class RoutingSnapshot:
    schema_version: int
    created_at: str
    name: str
    console: dict[str, str]
    userrout_in: dict[int, list | None]
    userrout_out: dict[int, list | None]
    # UNVERIFIED -- see app.osc.addresses module docstring. Keyed by the
    # (unconfirmed) address string itself rather than a channel/block
    # number, since that structure isn't confirmed either.
    routing_in_blocks: dict[str, list | None]
    card_out_blocks: dict[str, list | None]
    routing_addresses_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutingSnapshot":
        def _int_keys(d: dict) -> dict:
            return {int(k): v for k, v in d.items()}

        return cls(
            schema_version=data["schema_version"],
            created_at=data["created_at"],
            name=data["name"],
            console=data["console"],
            userrout_in=_int_keys(data["userrout_in"]),
            userrout_out=_int_keys(data["userrout_out"]),
            routing_in_blocks=data["routing_in_blocks"],
            card_out_blocks=data["card_out_blocks"],
            routing_addresses_verified=data.get("routing_addresses_verified", False),
        )


def read_routing_snapshot(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    name: str | None = None,
    correlation_id: str | None = None,
) -> RoutingSnapshot:
    """Query current routing state per CLAUDE.md step 3: all 32
    ``/config/userrout/in/NN`` and ``/config/userrout/out/NN`` (confirmed
    addresses), plus the block-level ``/config/routing/IN/*`` and CARD
    output blocks (best-effort, unverified -- see app.osc.addresses).
    """
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    name = name or datetime.now(timezone.utc).strftime("snapshot_%Y%m%dT%H%M%SZ")

    in_addrs = addresses.all_userrout_in()
    out_addrs = addresses.all_userrout_out()
    in_results = osc.query_many(in_addrs, correlation_id=correlation_id)
    out_results = osc.query_many(out_addrs, correlation_id=correlation_id)

    userrout_in = {
        ch: (list(in_results[addresses.userrout_in(ch)]) if in_results[addresses.userrout_in(ch)] is not None else None)
        for ch in range(1, addresses.NUM_CHANNELS + 1)
    }
    userrout_out = {
        ch: (list(out_results[addresses.userrout_out(ch)]) if out_results[addresses.userrout_out(ch)] is not None else None)
        for ch in range(1, addresses.NUM_CHANNELS + 1)
    }

    diagnostics.log_watchdog(
        "routing_block_addresses_unverified",
        {
            "note": (
                "Block-level routing addresses are best-effort placeholders, "
                "not confirmed against the Maillot doc or real hardware. "
                "Treat routing_in_blocks/card_out_blocks in this snapshot as "
                "informational only until verified."
            ),
        },
        correlation_id=correlation_id,
    )
    in_block_results = osc.query_many(addresses.ROUTING_IN_BLOCKS_TODO_VERIFY, correlation_id=correlation_id)
    card_out_block_results = osc.query_many(addresses.CARD_OUT_BLOCKS_TODO_VERIFY, correlation_id=correlation_id)

    routing_in_blocks = {addr: (list(v) if v is not None else None) for addr, v in in_block_results.items()}
    card_out_blocks = {addr: (list(v) if v is not None else None) for addr, v in card_out_block_results.items()}

    snapshot = RoutingSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        name=name,
        console=dict(osc.xinfo),
        userrout_in=userrout_in,
        userrout_out=userrout_out,
        routing_in_blocks=routing_in_blocks,
        card_out_blocks=card_out_blocks,
        routing_addresses_verified=addresses.ROUTING_ADDRESSES_VERIFIED,
    )

    diagnostics.log_state_change(
        "routing_snapshot_captured",
        after={
            "name": name,
            "missing_userrout_in": [ch for ch, v in userrout_in.items() if v is None],
            "missing_userrout_out": [ch for ch, v in userrout_out.items() if v is None],
        },
        correlation_id=correlation_id,
    )
    return snapshot


def save_snapshot(snapshot: RoutingSnapshot, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.name}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot.to_dict(), f, indent=2, default=str)
    return path


def load_snapshot(path: str | Path) -> RoutingSnapshot:
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    return RoutingSnapshot.from_dict(data)
