"""Routing apply/restore -- insert-based design.

Supersedes the earlier userrout/in-swap approach (which changed each
channel's *input* source to a Card return and flipped its containing block
to a User In bank). That did not work in practice, so routing was
re-thought around channel *inserts* instead -- confirmed against the target
console's own setup screens (Resources/*.png):

  Signal path per managed channel N (assigned Aux slot K, 1..6):
    console preamp -> Card output block = Local (1:1) -> PC input N
       -> notch filtering ->
    PC output K -> Card in K -> Aux In K (remapped from Card) ->
       channel N insert return (POST), replacing the strip signal.

  The console-side settings this writes (all snapshot-first, readback-
  verified, paced):
    1. /config/routing/CARD/<block> = Local  (AN..) -- so the PC can read
       each managed channel off the card.
    2. /config/routing/IN/AUX = Card 1-N  (rtina 10/11/12) -- the insert
       returns arrive from the PC on Card 1-N, remapped onto Aux In 1-N.
    3. /outputs/aux/K/src = Insert  -- only when the raw "Insert" value is
       known (AppConfig.aux_out_insert_src_value / addresses.AUX_OUT_SRC_
       INSERT); left to the desk otherwise, never guessed.
    4. /ch/N/insert/{pos=POST, sel=AUX K, on=ON}.

Gig-safety: a dead PC leaves a managed channel's insert return silent, so
restore = replay the snapshot (which turns every insert back to its
captured state, i.e. off) -- and per-channel bypass is a single
/ch/N/insert/on 0 write, no read-modify-write of anything larger.

`card_out_slot` on ChannelState is repurposed here to mean the PC-output /
Aux slot K (1..max_insert_channels), which the audio engine uses as the
output index it writes this channel's processed audio to.
"""
from __future__ import annotations

import time

from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot
from app.state import AppState

DEFAULT_MAX_INSERT_CHANNELS = addresses.NUM_AUX  # 6 Aux buses = hard hardware ceiling


class RoutingApplyError(Exception):
    pass


def _assign_aux_slots(
    selected_channels: list[int], state: AppState, max_channels: int
) -> dict[int, int]:
    """Give each selected channel an Aux/PC-output slot 1..max_channels,
    preserving any slot a channel already holds from earlier this session
    and handing new channels the lowest free slots. Raises if more channels
    are selected than there are Aux buses."""
    ordered = sorted(set(selected_channels))
    if len(ordered) > max_channels:
        raise RoutingApplyError(
            f"insert-based routing supports at most {max_channels} channels at once "
            f"(one Aux bus each); {len(ordered)} were selected"
        )

    assignments: dict[int, int] = {}
    taken: set[int] = set()
    # Keep existing assignments first so slot numbers stay stable across
    # re-applies (a channel already inserted keeps its Aux bus).
    for channel in ordered:
        existing = state.channels[channel].card_out_slot
        if existing is not None:
            assignments[channel] = existing
            taken.add(existing)
    for channel in ordered:
        if channel in assignments:
            continue
        slot = next(s for s in range(1, max_channels + 1) if s not in taken)
        assignments[channel] = slot
        taken.add(slot)
    return assignments


def apply_routing(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    selected_channels: list[int],
    snapshot: RoutingSnapshot,
    state: AppState,
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
    insert_src_value: int | None = None,
    max_channels: int = DEFAULT_MAX_INSERT_CHANNELS,
) -> dict[int, int]:
    """Insert each selected channel: assign it an Aux bus / Card-return slot,
    point the Card output block at Local, remap Aux In from the Card returns,
    optionally patch the Aux output to Insert, and switch the channel's
    insert on (POST). Returns {channel: aux_slot}. Raises RoutingApplyError
    if any write does not read back as expected."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    if insert_src_value is None:
        insert_src_value = addresses.AUX_OUT_SRC_INSERT

    assignments = _assign_aux_slots(selected_channels, state, max_channels)
    # Reserve slots immediately so a concurrent read sees them.
    for channel, slot in assignments.items():
        state.channels[channel].card_out_slot = slot

    max_slot = max(assignments.values())
    card_block_indices = sorted({(ch - 1) // 8 for ch in assignments})

    verifications: list[tuple[str, int]] = []

    # 1. Card output blocks -> Local (so the PC reads each managed channel).
    for block_index in card_block_indices:
        addr = addresses.ROUTING_CARD_BLOCKS[block_index]
        value = addresses.card_block_local_value(block_index)
        osc.send(addr, value, correlation_id=correlation_id)
        verifications.append((addr, value))
        time.sleep(pace_sec)

    # 2. Aux In remap <- Card 1-N (the insert returns from the PC).
    aux_in_value = addresses.aux_in_card_remap_value(max_slot)
    osc.send(addresses.ROUTING_IN_AUX, aux_in_value, correlation_id=correlation_id)
    verifications.append((addresses.ROUTING_IN_AUX, aux_in_value))
    time.sleep(pace_sec)

    # 3. Aux output -> Insert (only if the raw value is known; never guessed).
    aux_out_skipped: list[int] = []
    for channel, slot in assignments.items():
        if insert_src_value is None:
            aux_out_skipped.append(slot)
            continue
        addr = addresses.aux_out_src_addr(slot)
        osc.send(addr, insert_src_value, correlation_id=correlation_id)
        verifications.append((addr, insert_src_value))
        time.sleep(pace_sec)

    # 4. Channel insert: position + Aux selection, then switch on last.
    for channel, slot in assignments.items():
        pos_addr = addresses.channel_insert_pos_addr(channel)
        sel_addr = addresses.channel_insert_sel_addr(channel)
        sel_value = addresses.insert_sel_aux_value(slot)
        osc.send(pos_addr, addresses.INSERT_POS_POST, correlation_id=correlation_id)
        verifications.append((pos_addr, addresses.INSERT_POS_POST))
        time.sleep(pace_sec)
        osc.send(sel_addr, sel_value, correlation_id=correlation_id)
        verifications.append((sel_addr, sel_value))
        time.sleep(pace_sec)
    for channel in assignments:
        on_addr = addresses.channel_insert_on_addr(channel)
        osc.send(on_addr, addresses.INSERT_ON, correlation_id=correlation_id)
        verifications.append((on_addr, addresses.INSERT_ON))
        time.sleep(pace_sec)

    # 5. Read back everything written to confirm.
    mismatches: list[str] = []
    for addr, expected in verifications:
        actual = osc.query_until_match(addr, expected, correlation_id=correlation_id)
        if actual != expected:
            mismatches.append(f"{addr}: expected {expected}, got {actual}")

    if mismatches:
        error = RoutingApplyError(
            f"apply_routing: {len(mismatches)} address(es) failed to confirm: {mismatches}"
        )
        diagnostics.log_error(error, context="apply_routing", correlation_id=correlation_id)
        raise error

    for channel in assignments:
        state.channels[channel].inserted = True

    diagnostics.log_state_change(
        "routing_applied",
        after={
            "assignments": assignments,
            "aux_in_remap": aux_in_value,
            "aux_out_insert_written": insert_src_value is not None,
            "aux_out_src_skipped_slots": aux_out_skipped,
        },
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
    """Toggle a managed channel's insert on/off -- a single /ch/N/insert/on
    write (ON re-inserts, OFF bypasses back to the channel's own dry signal).
    Requires the channel to have been applied this session (so its Aux slot
    and insert config are already in place). Returns the new `inserted`
    state."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    channel_state = state.channels[channel]
    if channel_state.card_out_slot is None:
        raise RoutingApplyError(
            f"channel {channel} has not been applied this session, nothing to bypass/insert"
        )

    addr = addresses.channel_insert_on_addr(channel)
    target = addresses.INSERT_OFF if channel_state.inserted else addresses.INSERT_ON

    osc.send(addr, target, correlation_id=correlation_id)
    actual = osc.query_until_match(addr, target, correlation_id=correlation_id)
    if actual != target:
        error = RoutingApplyError(f"bypass_channel: {addr} expected {target}, got {actual}")
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
    """Replay a captured snapshot: userrout in/out, the routing blocks
    (which include the Aux-In remap and Card output blocks), each Aux
    output's src, and every channel's insert config -- putting inserts back
    to their captured (off) state. Non-fatal: logs and returns a list of
    address mismatches rather than raising, per CLAUDE.md's gig-safe
    principle -- a full restore is the last line of defense and must not
    abort partway through leaving a half-restored console."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    mismatches: list[str] = []
    verifications: list[tuple[str, int]] = []

    # -- userrout in/out (untouched by the insert design, but replayed for
    #    completeness so an old-design remnant is also cleaned up) ----------
    for channel in range(1, addresses.NUM_USERROUT_IN + 1):
        value = snapshot.userrout_in[channel - 1]
        if value is None:
            continue
        addr = addresses.userrout_in_addr(channel)
        osc.send(addr, value, correlation_id=correlation_id)
        verifications.append((addr, value))
        time.sleep(pace_sec)

    for channel in range(1, addresses.NUM_USERROUT_OUT + 1):
        value = snapshot.userrout_out[channel - 1]
        if value is None:
            continue
        addr = addresses.userrout_out_addr(channel)
        osc.send(addr, value, correlation_id=correlation_id)
        verifications.append((addr, value))
        time.sleep(pace_sec)

    # -- routing blocks (Aux-In remap + Card output blocks live here) -------
    for group, addr_table_pairs in addresses.ROUTING_GROUPS.items():
        values = snapshot.routing.get(group, [])
        for (addr, _table), value in zip(addr_table_pairs, values):
            if value is None:
                continue
            osc.send(addr, value, correlation_id=correlation_id)
            verifications.append((addr, value))
            time.sleep(pace_sec)

    # -- Aux output src -----------------------------------------------------
    for aux_index, value in enumerate(snapshot.aux_out_src):
        if value is None:
            continue
        addr = addresses.aux_out_src_addr(aux_index + 1)
        osc.send(addr, value, correlation_id=correlation_id)
        verifications.append((addr, value))
        time.sleep(pace_sec)

    # -- channel inserts: replay captured config; force off anything we
    #    turned on but have no captured value for (gig-safe) ----------------
    restored_channels: set[int] = set()
    for ch_index, insert in enumerate(snapshot.channel_inserts):
        channel = ch_index + 1
        if not insert:
            continue
        restored_channels.add(channel)
        for field, addr_fn in (
            ("on", addresses.channel_insert_on_addr),
            ("pos", addresses.channel_insert_pos_addr),
            ("sel", addresses.channel_insert_sel_addr),
        ):
            value = insert.get(field)
            if value is None:
                continue
            addr = addr_fn(channel)
            osc.send(addr, value, correlation_id=correlation_id)
            verifications.append((addr, value))
            time.sleep(pace_sec)

    if state is not None:
        for channel, channel_state in state.channels.items():
            if channel_state.card_out_slot is None or channel in restored_channels:
                continue
            # We inserted this channel but the snapshot has no captured
            # insert to restore -- switch it off rather than leave it hanging.
            addr = addresses.channel_insert_on_addr(channel)
            osc.send(addr, addresses.INSERT_OFF, correlation_id=correlation_id)
            verifications.append((addr, addresses.INSERT_OFF))
            time.sleep(pace_sec)

    # -- verify --------------------------------------------------------------
    for addr, value in verifications:
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
