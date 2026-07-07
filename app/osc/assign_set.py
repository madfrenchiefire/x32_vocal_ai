"""Assign-set (Set A/B) provisioning.

Address shape (`/config/ctrl/<A|B>/<enc|btn>/<N>`) follows CLAUDE.md's Slot
model: 4 encoders (sensitivity) + 8 buttons (btn 1-4 = AI on/off, btn 5-8 =
insert/bypass) per set, Set A covering channel slots 1-4, Set B covering
slots 5-8.

**The assignment value format is NOT confirmed** -- CLAUDE.md flags this
explicitly ("Exact string encoding of MIDI assignments: verify against the
Patrick-Gilles Maillot unofficial X32 OSC document, or empirically"). This
module reads, writes, and restores whatever raw value is there or
supplied -- it does not construct or guess the assignment value itself.
Get the real format by assigning a control on the console's Setup > Remote
screen, querying the same address, and copying exactly what comes back;
only then wire a real value into app.midi.service's provisioning step.

Set C is off-limits per CLAUDE.md's core design principle #4 -- VALID_SETS
only ever contains "A" and "B", and there is no function here that can
address Set C.
"""
from __future__ import annotations

from typing import Any

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection

VALID_SETS = ("A", "B")
NUM_ENCODERS_PER_SET = 4
NUM_BUTTONS_PER_SET = 8


class AssignSetError(Exception):
    pass


def _check_set(set_name: str) -> None:
    if set_name not in VALID_SETS:
        raise ValueError(f"set_name must be 'A' or 'B' (Set C is off-limits), got {set_name!r}")


def encoder_addr(set_name: str, index: int) -> str:
    _check_set(set_name)
    if not 1 <= index <= NUM_ENCODERS_PER_SET:
        raise ValueError(f"encoder index must be 1-{NUM_ENCODERS_PER_SET}, got {index}")
    return f"/config/ctrl/{set_name}/enc/{index}"


def button_addr(set_name: str, index: int) -> str:
    _check_set(set_name)
    if not 1 <= index <= NUM_BUTTONS_PER_SET:
        raise ValueError(f"button index must be 1-{NUM_BUTTONS_PER_SET}, got {index}")
    return f"/config/ctrl/{set_name}/btn/{index}"


def all_assign_set_addresses() -> list[str]:
    addrs = []
    for set_name in VALID_SETS:
        addrs += [encoder_addr(set_name, i) for i in range(1, NUM_ENCODERS_PER_SET + 1)]
        addrs += [button_addr(set_name, i) for i in range(1, NUM_BUTTONS_PER_SET + 1)]
    return addrs


def snapshot_assign_sets(
    osc: OscConnection, diagnostics: DiagnosticsLogger, correlation_id: str | None = None
) -> dict[str, tuple | None]:
    """Read every Set A/B encoder + button assignment currently on the
    console, before this app writes anything -- per CLAUDE.md's "snapshot
    before touching anything" principle. Values are opaque raw tuples."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    results = osc.query_many(all_assign_set_addresses(), correlation_id=correlation_id)
    diagnostics.log_state_change(
        "assign_sets_snapshotted",
        after={"missing": [addr for addr, value in results.items() if value is None]},
        correlation_id=correlation_id,
    )
    return results


def write_assignment(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    address: str,
    *args: Any,
    correlation_id: str | None = None,
) -> None:
    """Write a single control's assignment. `args` must already be in the
    console's expected format -- this function does not construct,
    interpret, or validate it (see module docstring)."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    osc.send(address, *args, correlation_id=correlation_id)
    expected = args[0] if len(args) == 1 else args
    actual = osc.query_until_match(address, expected, correlation_id=correlation_id)
    if actual != expected:
        error = AssignSetError(f"{address}: expected {expected!r}, got {actual!r}")
        diagnostics.log_error(error, context="write_assignment", correlation_id=correlation_id)
        raise error
    diagnostics.log_state_change(
        "assign_set_written", after={"address": address, "value": expected}, correlation_id=correlation_id
    )


def restore_assignments(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    snapshot: dict[str, tuple | None],
    correlation_id: str | None = None,
) -> list[str]:
    """Replay a previously captured assign-set snapshot. Non-fatal on
    mismatch, matching app.osc.routing_apply.restore_snapshot's gig-safe
    behavior -- logs and reports, doesn't raise partway through."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    mismatches: list[str] = []
    for address, value in snapshot.items():
        if value is None:
            continue
        osc.send(address, *value, correlation_id=correlation_id)

    for address, value in snapshot.items():
        if value is None:
            continue
        expected = value[0] if len(value) == 1 else value
        actual = osc.query_until_match(address, expected, correlation_id=correlation_id)
        if actual != expected:
            mismatches.append(f"{address}: expected {expected!r}, got {actual!r}")

    if mismatches:
        diagnostics.log_error(
            AssignSetError(f"restore_assignments: {len(mismatches)} address(es) did not confirm"),
            context="restore_assignments",
            correlation_id=correlation_id,
        )
    else:
        diagnostics.log_state_change("assign_sets_restored", correlation_id=correlation_id)

    return mismatches
