"""Routing snapshot: read the console's current routing state and persist
it as a named JSON file.

Per CLAUDE.md step 2 of routing automation ("Snapshot before touching
anything"): this is the read side only. Nothing here writes to the
console. Applying/restoring a snapshot is a separate, not-yet-implemented
module (app.osc.routing_apply).

Address shapes are confirmed from a real console scene file -- see
app.osc.addresses module docstring. ``/config/userrout/in`` and
``/config/userrout/out`` are each a single address carrying the full
32-/48-element array (not one address per channel), and the six
``/config/routing/*`` block nodes are each a single address carrying an
array of per-8-channel-block source tokens.
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

SNAPSHOT_SCHEMA_VERSION = 2


@dataclass
class RoutingSnapshot:
    schema_version: int
    created_at: str
    name: str
    console: dict[str, str]
    userrout_in: list | None
    userrout_out: list | None
    # Keyed by app.osc.addresses.ROUTING_BLOCK_ADDRESSES' short names
    # (rec, in, aes50a, aes50b, card, out, play).
    routing: dict[str, list | None]
    routing_addresses_verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutingSnapshot":
        return cls(
            schema_version=data["schema_version"],
            created_at=data["created_at"],
            name=data["name"],
            console=data["console"],
            userrout_in=data["userrout_in"],
            userrout_out=data["userrout_out"],
            routing=data["routing"],
            routing_addresses_verified=data.get("routing_addresses_verified", True),
        )


def read_routing_snapshot(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    name: str | None = None,
    correlation_id: str | None = None,
) -> RoutingSnapshot:
    """Query current routing state: the bulk ``/config/userrout/in`` and
    ``/config/userrout/out`` arrays, plus the six confirmed
    ``/config/routing/*`` block nodes (see app.osc.addresses).
    """
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    name = name or datetime.now(timezone.utc).strftime("snapshot_%Y%m%dT%H%M%SZ")

    userrout_addrs = [addresses.USERROUT_IN, addresses.USERROUT_OUT]
    routing_addrs = list(addresses.ROUTING_BLOCK_ADDRESSES.values())

    userrout_results = osc.query_many(userrout_addrs, correlation_id=correlation_id)
    routing_results = osc.query_many(routing_addrs, correlation_id=correlation_id)

    userrout_in = _as_list(userrout_results[addresses.USERROUT_IN])
    userrout_out = _as_list(userrout_results[addresses.USERROUT_OUT])
    routing = {
        short_name: _as_list(routing_results[addr])
        for short_name, addr in addresses.ROUTING_BLOCK_ADDRESSES.items()
    }

    snapshot = RoutingSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        name=name,
        console=dict(osc.xinfo),
        userrout_in=userrout_in,
        userrout_out=userrout_out,
        routing=routing,
        routing_addresses_verified=addresses.ROUTING_ADDRESSES_VERIFIED,
    )

    diagnostics.log_state_change(
        "routing_snapshot_captured",
        after={
            "name": name,
            "userrout_in_captured": userrout_in is not None,
            "userrout_out_captured": userrout_out is not None,
            "missing_routing_blocks": [k for k, v in routing.items() if v is None],
        },
        correlation_id=correlation_id,
    )
    return snapshot


def _as_list(args: tuple | None) -> list | None:
    return list(args) if args is not None else None


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
