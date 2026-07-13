"""Assign-set (Set A/B) provisioning.

Address shape: `/config/userctrl/<A|B>/<enc|btn>/<N>` -- **confirmed against
real hardware for encoders (2026-07-07, firmware 4.13)** via the /xremote
sniffer (`app.osc.protocol_discovery.sniff_pushed_changes`): changing Set A
encoder assignments on a live console pushed `/config/userctrl/A/enc/1`..
`/enc/3` with string values (`'X000'`, `'S0000'`, `'MC01000'`, `'MC03000'`,
...). The originally guessed `/config/ctrl/...` shape got no reply on the
same console and is wrong. **Button numbering confirmed by a second sniff
the same day**: changing Set A button assignments pushed
`/config/userctrl/A/btn/5` and `/btn/6` -- buttons continue the numbering
past the 4 encoders (btn/5-12; 5 and 6 observed directly, 7-12 by that
now-verified pattern).

**The assignment value *string format* is partially observed, not
decoded**: `'MC01000'`/`'MC02001'`/`'MC03002'` correlate with MIDI-CC-type
assignments, but which digit group is the CC number vs the MIDI channel is
not yet pinned down -- both fields incremented together in the observed
data, so either reading fits. A button read `'Mc00000'` with a *lowercase*
'c', so letter case apparently encodes an assignment sub-type (CC vs
CC-toggle vs note..., un-decoded). `'S0000'`/`'S5000'`/`'X000'` are other
assignment types, un-decoded.

**MIDI-CC value format decoded (2026-07-07)** by matching the sniffed
strings against a screenshot of the console's own Edit Assigns screen for
the same state, and since **doc-confirmed** (X32_OSC.pdf, committed in
this repo -- User ASSIGN Section chapter: buttons/encoders take strings
like `"Mxyyzzz"`, x = message type `C`/`N`/`P` = Ctrl Chg/Note/Prog Chg
for "Midi Push" or lowercase `c`/`n` for "Midi Toggle", yy = MIDI channel
01-16, zzz = value 000-127):

    'M' + ('C' push | 'c' toggle) + <MIDI channel, 2 digits, 1-based>
        + <CC number, 3 digits>

:func:`midi_cc_value` constructs these; snapshot/restore still treats
values as opaque (the doc also specifies every non-MIDI assignment type --
'F' fader, 'P' pan/page, 'S' send, 'X' effect, 'O' mute, 'I' insert, 'R'
remote, 'D' selected-channel -- none of which this app constructs). One
observed quirk feeding the always-readback-verify rule: two controls
whose GUI showed "Channel 01" pushed a channel field of `'00'` (probably
the console's internal default before the channel dropdown is first
touched), so a write must be confirmed by readback (write_assignment
already does) rather than assumed.

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
# Buttons continue the numbering after the 4 encoders: btn/5 .. btn/12.
BUTTON_INDICES = tuple(range(5, 13))


class AssignSetError(Exception):
    pass


def _check_set(set_name: str) -> None:
    if set_name not in VALID_SETS:
        raise ValueError(f"set_name must be 'A' or 'B' (Set C is off-limits), got {set_name!r}")


def encoder_addr(set_name: str, index: int) -> str:
    _check_set(set_name)
    if not 1 <= index <= NUM_ENCODERS_PER_SET:
        raise ValueError(f"encoder index must be 1-{NUM_ENCODERS_PER_SET}, got {index}")
    return f"/config/userctrl/{set_name}/enc/{index}"


def button_addr(set_name: str, index: int) -> str:
    _check_set(set_name)
    if index not in BUTTON_INDICES:
        raise ValueError(f"button index must be {BUTTON_INDICES[0]}-{BUTTON_INDICES[-1]}, got {index}")
    return f"/config/userctrl/{set_name}/btn/{index}"


def all_assign_set_addresses() -> list[str]:
    addrs = []
    for set_name in VALID_SETS:
        addrs += [encoder_addr(set_name, i) for i in range(1, NUM_ENCODERS_PER_SET + 1)]
        addrs += [button_addr(set_name, i) for i in BUTTON_INDICES]
    return addrs


def midi_cc_value(midi_channel: int, cc: int, toggle: bool = False) -> str:
    """Construct a MIDI-Ctrl-Chg assignment string in the decoded format
    (see module docstring): e.g. midi_cc_value(16, 11) == 'MC16011'.
    toggle=True produces the lowercase-'c' "Midi Toggle" variant; the
    default is "Midi Push" (CC 127 on press / 0 on release), which is what
    this app's own CC dispatch expects for buttons -- every press sends a
    127 -- whereas a console-side Toggle would only send 127 on alternate
    presses, halving the app's toggle rate."""
    if not 1 <= midi_channel <= 16:
        raise ValueError(f"midi_channel must be 1-16, got {midi_channel}")
    if not 0 <= cc <= 127:
        raise ValueError(f"cc must be 0-127, got {cc}")
    return f"M{'c' if toggle else 'C'}{midi_channel:02d}{cc:03d}"


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
