"""Routing snapshot: read the console's current routing state and persist
it as a named JSON file.

Per CLAUDE.md step 2 of routing automation ("Snapshot before touching
anything"): this is the read side only. Nothing here writes to the
console. Applying/restoring a snapshot is a separate, not-yet-implemented
module (app.osc.routing_apply).

Address shapes are confirmed from Patrick-Gilles Maillot's own
reverse-engineered parameter table (github.com/pmaillot/X32-Behringer) --
see app.osc.addresses module docstring. Individual per-channel/per-block
addresses (flagged F_XET = get+set in that table) are queried first; the
bulk parent address for a group (F_FND, confirmed to appear in .scn scene
dumps) is only tried as a fallback if one or more individual queries in
that group time out.
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

SNAPSHOT_SCHEMA_VERSION = 3


@dataclass
class RoutingSnapshot:
    schema_version: int
    created_at: str
    name: str
    console: dict[str, str]
    userrout_in: list  # 32 raw ints (or None per entry if unreachable)
    userrout_out: list  # 48 raw ints (or None per entry if unreachable)
    # Keyed by app.osc.addresses.ROUTING_GROUPS' group names (routswitch,
    # in, aes50a, aes50b, card, out, play); each value is the group's raw
    # enum ints in the same order as ROUTING_GROUPS[group].
    routing: dict[str, list]
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

    def decode_routing(self) -> dict[str, list]:
        """Decode raw routing enum ints into their display tokens (e.g.
        16 -> "CARD1-8") using app.osc.addresses.ROUTING_ENUM_TABLES. The
        raw ints in self.routing remain the source of truth; this is a
        read-only convenience view, never stored in the snapshot file."""
        decoded: dict[str, list] = {}
        for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
            values = self.routing.get(group, [])
            decoded[group] = [
                addresses.decode_routing_value(table, v)
                for (_addr, table), v in zip(addr_table_pairs, values)
            ]
        return decoded

    def decode_userrout_in(self) -> list:
        """Decode each channel's raw userrout/in int via
        app.osc.addresses.decode_userrout_value(), e.g. 34 -> "AES50-A 2"."""
        return [addresses.decode_userrout_value(v) for v in self.userrout_in]

    def decode_userrout_out(self) -> list:
        return [addresses.decode_userrout_value(v) for v in self.userrout_out]


def read_routing_snapshot(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    name: str | None = None,
    correlation_id: str | None = None,
) -> RoutingSnapshot:
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    name = name or datetime.now(timezone.utc).strftime("snapshot_%Y%m%dT%H%M%SZ")

    userrout_in = _query_with_bulk_fallback(
        osc, diagnostics, addresses.ALL_USERROUT_IN, addresses.USERROUT_IN,
        addresses.NUM_USERROUT_IN, correlation_id,
    )
    userrout_out = _query_with_bulk_fallback(
        osc, diagnostics, addresses.ALL_USERROUT_OUT, addresses.USERROUT_OUT,
        addresses.NUM_USERROUT_OUT, correlation_id,
    )

    routing: dict[str, list] = {}
    for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
        group_addrs = [addr for addr, _table in addr_table_pairs]
        bulk_addr = addresses.ROUTING_GROUP_BULK_ADDR[group]
        routing[group] = _query_with_bulk_fallback(
            osc, diagnostics, group_addrs, bulk_addr, len(group_addrs), correlation_id,
        )

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
            "missing_userrout_in": [i + 1 for i, v in enumerate(userrout_in) if v is None],
            "missing_userrout_out": [i + 1 for i, v in enumerate(userrout_out) if v is None],
            "missing_routing_groups": {g: vals.count(None) for g, vals in routing.items() if None in vals},
        },
        correlation_id=correlation_id,
    )
    return snapshot


def _query_with_bulk_fallback(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    individual_addrs: list[str],
    bulk_addr: str,
    expected_len: int,
    correlation_id: str,
) -> list:
    """Query each individual address (primary, confirmed get+set per
    Maillot's table). If any are missing, opportunistically try the bulk
    parent address once and use it to fill the gaps -- only if it replies
    with exactly expected_len integers, since whether the console answers
    a bare query on the bulk node at all is unconfirmed."""
    results = osc.query_many(individual_addrs, correlation_id=correlation_id)
    values = [_first(results[addr]) for addr in individual_addrs]

    if any(v is None for v in values):
        bulk_reply = osc.query_many([bulk_addr], correlation_id=correlation_id)[bulk_addr]
        if bulk_reply is not None and len(bulk_reply) == expected_len and all(
            isinstance(v, int) for v in bulk_reply
        ):
            diagnostics.log_watchdog(
                "routing_bulk_fallback_used",
                {
                    "bulk_address": bulk_addr,
                    "missing_before_fallback": sum(1 for v in values if v is None),
                },
                correlation_id=correlation_id,
            )
            values = [existing if existing is not None else bulk_reply[i] for i, existing in enumerate(values)]

    return values


def _first(args: tuple | None):
    if args is None:
        return None
    return args[0] if len(args) == 1 else list(args)


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
