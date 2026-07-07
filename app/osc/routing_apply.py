"""Routing apply/restore.

Implements CLAUDE.md's routing-automation design: stage every per-channel
`userrout/in` write first, flip the containing blocks to the matching User
In bank last, pace writes, and read back to confirm (with the settle-delay
retry already built into `OscConnection.query_until_match`).

Safety note this module exists specifically to handle: flipping an 8-channel
block to User In affects *all 8* channels in it, not just the one being
inserted. Any channel in that block that isn't explicitly selected has its
current physical source replicated into its own `userrout/in` slot first
(`addresses.equivalent_userrout_in_value`), so it keeps getting the same
audio it always had -- just routed via User In instead of directly. Without
this, an unselected channel sharing a freshly-flipped block would go silent
(its `userrout/in` would still read 0/UNSET).
"""
from __future__ import annotations

import time

from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot
from app.state import AppState

CARD_BASE_VALUE = 128  # userrout value for "Card channel 0" -- Card N = 128 + N


class RoutingApplyError(Exception):
    pass


def _card_slot_for_channel(state: AppState) -> int:
    used = {c.card_out_slot for c in state.channels.values() if c.card_out_slot is not None}
    for slot in range(1, addresses.NUM_USERROUT_IN + 1):
        if slot not in used:
            return slot
    raise RoutingApplyError("no free Card channel slots available")


def apply_routing(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    selected_channels: list[int],
    snapshot: RoutingSnapshot,
    state: AppState,
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
) -> dict[int, int]:
    """Insert each of selected_channels: assign it a Card return slot (reusing
    one already assigned this session if present), write its userrout/in to
    that Card value, and flip its block to the matching User In bank --
    replicating the pre-flip physical source for any other channel sharing
    that block. Returns {channel: card_slot}. Raises RoutingApplyError if any
    write doesn't read back as expected."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()

    assignments: dict[int, int] = {}
    for channel in selected_channels:
        existing = state.channels[channel].card_out_slot
        assignments[channel] = existing if existing is not None else _card_slot_for_channel(state)
        if existing is None:
            # Reserve it immediately so the next iteration doesn't reuse it.
            state.channels[channel].card_out_slot = assignments[channel]

    touched_block_indices = sorted({(ch - 1) // 8 for ch in selected_channels})
    passthrough_writes: dict[int, int] = {}
    for block_index in touched_block_indices:
        block_raw = snapshot.routing.get("in", [None] * 4)[block_index]
        if block_raw is None:
            continue
        block_channels = range(block_index * 8 + 1, block_index * 8 + 9)
        for channel in block_channels:
            if channel in assignments:
                continue
            equivalent = addresses.equivalent_userrout_in_value(block_raw, channel)
            if equivalent is not None:
                passthrough_writes[channel] = equivalent

    # 1. Stage every per-channel userrout/in write first.
    for channel, card_slot in assignments.items():
        osc.send(addresses.userrout_in_addr(channel), CARD_BASE_VALUE + card_slot, correlation_id=correlation_id)
        time.sleep(pace_sec)
    for channel, value in passthrough_writes.items():
        osc.send(addresses.userrout_in_addr(channel), value, correlation_id=correlation_id)
        time.sleep(pace_sec)

    # 2. Flip blocks to the matching User In bank last.
    for block_index in touched_block_indices:
        block_addr = addresses.ROUTING_IN_BLOCKS[block_index]
        block_value = addresses.USER_IN_BASE_VALUE + block_index
        osc.send(block_addr, block_value, correlation_id=correlation_id)
        time.sleep(pace_sec)

    # 3. Read back everything to confirm.
    mismatches: list[str] = []
    for channel, card_slot in assignments.items():
        expected = CARD_BASE_VALUE + card_slot
        addr = addresses.userrout_in_addr(channel)
        actual = osc.query_until_match(addr, expected, correlation_id=correlation_id)
        if actual != expected:
            mismatches.append(f"{addr}: expected {expected}, got {actual}")
    for channel, value in passthrough_writes.items():
        addr = addresses.userrout_in_addr(channel)
        actual = osc.query_until_match(addr, value, correlation_id=correlation_id)
        if actual != value:
            mismatches.append(f"{addr}: expected {value}, got {actual}")
    for block_index in touched_block_indices:
        block_addr = addresses.ROUTING_IN_BLOCKS[block_index]
        expected = addresses.USER_IN_BASE_VALUE + block_index
        actual = osc.query_until_match(block_addr, expected, correlation_id=correlation_id)
        if actual != expected:
            mismatches.append(f"{block_addr}: expected {expected}, got {actual}")

    if mismatches:
        error = RoutingApplyError(f"apply_routing: {len(mismatches)} address(es) failed to confirm: {mismatches}")
        diagnostics.log_error(error, context="apply_routing", correlation_id=correlation_id)
        raise error

    for channel, card_slot in assignments.items():
        state.channels[channel].inserted = True

    diagnostics.log_state_change(
        "routing_applied",
        after={"assignments": assignments, "passthrough_channels": list(passthrough_writes)},
        correlation_id=correlation_id,
    )
    return assignments


def bypass_channel(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    snapshot: RoutingSnapshot,
    state: AppState,
    correlation_id: str | None = None,
) -> bool:
    """Toggle: if inserted, write userrout/in back to its snapshot value
    (bypass). If bypassed, re-insert using its last-known Card slot. Assumes
    the channel's block is already on the matching User In bank (true for a
    channel apply_routing has touched). Returns the new `inserted` state."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    channel_state = state.channels[channel]
    addr = addresses.userrout_in_addr(channel)

    if channel_state.inserted:
        original_value = snapshot.userrout_in[channel - 1]
        if original_value is None:
            raise RoutingApplyError(f"no snapshot value for channel {channel}, refusing to bypass blind")
        target_value = original_value
    else:
        if channel_state.card_out_slot is None:
            raise RoutingApplyError(f"channel {channel} has never been inserted this session, nothing to re-insert")
        target_value = CARD_BASE_VALUE + channel_state.card_out_slot

    osc.send(addr, target_value, correlation_id=correlation_id)
    actual = osc.query_until_match(addr, target_value, correlation_id=correlation_id)
    if actual != target_value:
        error = RoutingApplyError(f"bypass_channel: {addr} expected {target_value}, got {actual}")
        diagnostics.log_error(error, context="bypass_channel", correlation_id=correlation_id)
        raise error

    channel_state.inserted = not channel_state.inserted
    diagnostics.log_state_change(
        "channel_bypass_toggled",
        after={"channel": channel, "inserted": channel_state.inserted},
        correlation_id=correlation_id,
    )
    return channel_state.inserted


def restore_snapshot(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    snapshot: RoutingSnapshot,
    state: AppState | None = None,
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
) -> list[str]:
    """Replay every captured userrout/in, userrout/out, and routing-block
    value from the snapshot. Non-fatal: logs and returns a list of address
    mismatches rather than raising, per CLAUDE.md's gig-safe principle --
    a full restore is itself the last line of defense and shouldn't abort
    partway through leaving the console in a worse, half-restored state."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    mismatches: list[str] = []

    for channel in range(1, addresses.NUM_USERROUT_IN + 1):
        value = snapshot.userrout_in[channel - 1]
        if value is None:
            continue
        addr = addresses.userrout_in_addr(channel)
        osc.send(addr, value, correlation_id=correlation_id)
        time.sleep(pace_sec)

    for channel in range(1, addresses.NUM_USERROUT_OUT + 1):
        value = snapshot.userrout_out[channel - 1]
        if value is None:
            continue
        addr = addresses.userrout_out_addr(channel)
        osc.send(addr, value, correlation_id=correlation_id)
        time.sleep(pace_sec)

    for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
        values = snapshot.routing.get(group, [])
        for (addr, _table), value in zip(addr_table_pairs, values):
            if value is None:
                continue
            osc.send(addr, value, correlation_id=correlation_id)
            time.sleep(pace_sec)

    for channel in range(1, addresses.NUM_USERROUT_IN + 1):
        value = snapshot.userrout_in[channel - 1]
        if value is None:
            continue
        addr = addresses.userrout_in_addr(channel)
        actual = osc.query_until_match(addr, value, correlation_id=correlation_id)
        if actual != value:
            mismatches.append(f"{addr}: expected {value}, got {actual}")

    for channel in range(1, addresses.NUM_USERROUT_OUT + 1):
        value = snapshot.userrout_out[channel - 1]
        if value is None:
            continue
        addr = addresses.userrout_out_addr(channel)
        actual = osc.query_until_match(addr, value, correlation_id=correlation_id)
        if actual != value:
            mismatches.append(f"{addr}: expected {value}, got {actual}")

    for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
        values = snapshot.routing.get(group, [])
        for (addr, _table), value in zip(addr_table_pairs, values):
            if value is None:
                continue
            actual = osc.query_until_match(addr, value, correlation_id=correlation_id)
            if actual != value:
                mismatches.append(f"{addr}: expected {value}, got {actual}")

    if mismatches:
        diagnostics.log_error(
            RoutingApplyError(f"restore_snapshot: {len(mismatches)} address(es) did not confirm"),
            context="restore_snapshot",
            correlation_id=correlation_id,
        )
    else:
        diagnostics.log_state_change(
            "snapshot_restored", after={"name": snapshot.name}, correlation_id=correlation_id
        )

    if state is not None:
        for channel_state in state.channels.values():
            channel_state.inserted = False
            channel_state.card_out_slot = None

    return mismatches
