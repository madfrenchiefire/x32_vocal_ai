"""One-stop troubleshooting/protocol-discovery capture against a real console.

CLAUDE.md's "Open items to verify" section lists several values this
project has never independently confirmed against real hardware (the raw
`userrout/out` value for "Main L/R", the MIDI-assignment string format for
`/config/ctrl/*`, the `/meters` blob layout). Confirming any of them has
always meant the same manual loop: read a baseline, go change something on
the console, read again, diff. This module is that loop, generalized and
made reusable -- both for the fully-passive stuff a console will always
answer right now (:func:`capture_full_state`) and for the "go touch the
console, we'll detect what changed" stuff that genuinely needs a human
(:func:`watch_until_changed`, :func:`capture_meters_sample`).

The CLI wizard built on top of these (`python -m app.tools.diagnose_console`)
is the actual "one tool" -- this module is its testable core.
"""
from __future__ import annotations

import queue
import time
from typing import Any, Callable

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.assign_set import snapshot_assign_sets
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import read_routing_snapshot
from app.osc.scribble_strip import read_all_channel_configs


def capture_full_state(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Everything the console will answer right now, in one call: xinfo, a
    full routing snapshot, the Set A/B assign-set snapshot, and every
    channel's scribble-strip name/color. This is the passive half of
    troubleshooting; see watch_until_changed()/capture_meters_sample() for
    the still-unconfirmed values that need a human to change something on
    the console while this app watches."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    routing = read_routing_snapshot(osc, diagnostics, name="protocol_discovery", correlation_id=correlation_id)
    assign_sets = snapshot_assign_sets(osc, diagnostics, correlation_id=correlation_id)
    channel_configs = read_all_channel_configs(osc, correlation_id=correlation_id)
    return {
        "xinfo": dict(osc.xinfo),
        "routing_snapshot": routing.to_dict(),
        "assign_sets": assign_sets,
        "channel_configs": channel_configs,
    }


def watch_until_changed(
    osc: OscConnection,
    addresses_: list[str],
    diagnostics: DiagnosticsLogger,
    poll_interval_sec: float = 0.3,
    timeout_sec: float = 60.0,
    correlation_id: str | None = None,
    on_tick: Callable[[float], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read addresses_ once as a baseline, then keep re-reading them every
    poll_interval_sec until at least one value differs from its baseline
    or timeout_sec elapses -- the primitive behind every "go change X on
    the console, we'll detect what value that produces" step (Main L/R
    echo-reference value, MIDI assign-set format, any other still-
    unconfirmed enum value). Returns {address: {"before", "after"}} for
    whichever address(es) changed; empty if nothing changed before
    timeout. on_tick(elapsed_sec), if given, is called once per poll so a
    CLI can show a countdown while the human goes and makes the change."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    baseline = osc.query_many(addresses_, correlation_id=correlation_id)

    start = time.monotonic()
    changed: dict[str, dict[str, Any]] = {}
    while time.monotonic() - start < timeout_sec:
        if on_tick is not None:
            on_tick(time.monotonic() - start)
        time.sleep(poll_interval_sec)
        current = osc.query_many(addresses_, correlation_id=correlation_id)
        for addr in addresses_:
            if current[addr] is not None and current[addr] != baseline[addr]:
                changed[addr] = {"before": baseline[addr], "after": current[addr]}
        if changed:
            break

    if changed:
        diagnostics.log_state_change(
            "protocol_discovery_change_detected", after=changed, correlation_id=correlation_id,
        )
    return changed


def sniff_pushed_changes(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    duration_sec: float = 60.0,
    correlation_id: str | None = None,
    on_tick: Callable[[float], None] | None = None,
) -> list[tuple[str, tuple]]:
    """Record every message the console sends us over duration_sec,
    whatever its address. With the /xremote keepalive active (always, on a
    connected OscConnection), the console pushes address+value for
    anything changed on the console surface -- so unlike
    watch_until_changed (which can only poll addresses it already knows),
    this discovers addresses this project has never seen: have a human
    change the control in question on the desk while this runs, and
    whatever address it lives at shows up in the returned
    (address, args) list. Confirmed necessary by a real-console capture
    (2026-07-07, firmware 4.13) where all 24 guessed
    /config/ctrl/A|B/enc|btn/N addresses returned nothing even to passive
    queries -- polling guessed addresses can't discover the right ones.

    Ctrl+C stops early and returns whatever was captured so far rather
    than discarding it -- these runs involve a human standing at a console,
    and re-doing the physical step because the tool threw away the data is
    exactly the failure mode this tool exists to avoid."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    q: queue.Queue = queue.Queue()
    osc.add_sniffer(q)
    messages: list[tuple[str, tuple]] = []
    start = time.monotonic()
    try:
        while time.monotonic() - start < duration_sec:
            if on_tick is not None:
                on_tick(time.monotonic() - start)
            try:
                messages.append(q.get(timeout=0.25))
            except queue.Empty:
                continue
    except KeyboardInterrupt:
        pass  # keep what we already captured -- see docstring
    finally:
        osc.remove_sniffer(q)

    diagnostics.log_state_change(
        "protocol_discovery_sniff",
        after={
            "message_count": len(messages),
            "addresses": sorted({addr for addr, _args in messages}),
        },
        correlation_id=correlation_id,
    )
    return messages


def capture_meters_sample(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    meter_path: str = "/meters/1",
    extra_args: tuple = (),
    listen_sec: float = 3.0,
    correlation_id: str | None = None,
) -> list[tuple]:
    """Best-effort capture of whatever the console sends back after a
    /meters subscribe attempt. Subscribe form per Maillot's unofficial X32
    OSC doc: send the *parent* /meters address with the wanted blob path
    as a string argument (`/meters ,s "/meters/1"`), after which the
    console streams blob messages on that path for ~10s; some meter paths
    take extra numeric args after the path (channel id etc.), hence
    extra_args. This form is from the doc, not yet confirmed against our
    own hardware -- a first attempt using a plain int subscribe on the
    blob path itself got zero replies on a real 4.13 console (2026-07-07),
    which is what prompted switching to the documented form. The blob
    layout is still unconfirmed either way, so nothing here decodes; raw
    reply args (including blob bytes) are returned for offline analysis.
    An empty return means this combination produced nothing within
    listen_sec, not necessarily that meters don't work on this console."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    osc.send("/meters", meter_path, *extra_args, correlation_id=correlation_id)
    messages = osc.listen(meter_path, listen_sec)
    diagnostics.log_state_change(
        "protocol_discovery_meters_capture",
        after={"meter_path": meter_path, "extra_args": list(extra_args), "message_count": len(messages)},
        correlation_id=correlation_id,
    )
    return messages
