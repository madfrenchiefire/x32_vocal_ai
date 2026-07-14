"""Console channel EQ writes: "burn" the app's notches into the desk.

After a ring-out session, the notch frequencies the app found live only in
the app's own filter bank -- pull the PC out of the audio path and the
suppression is gone. This module writes the worst offenders into the
channel's own 4-band parametric EQ (`/ch/NN/eq/...`), so the result of a
ring-out persists on the console itself with the app fully disengaged.

Addresses and value semantics from X32_OSC.pdf (committed in this repo):

    /ch/NN/eq/on          enum {OFF, ON}
    /ch/NN/eq/[1-4]/type  enum {LCut, LShv, PEQ, VEQ, HShv, HCut}
    /ch/NN/eq/[1-4]/f     logf [20, 20000, 201 steps] Hz
    /ch/NN/eq/[1-4]/g     linf [-15, +15, 0.25] dB
    /ch/NN/eq/[1-4]/q     logf [10 .. 0.3, 72 steps]  (note: inverted)

All float parameters travel over OSC as NORMALIZED 0.0-1.0 values (the
X32-wide convention, doc-confirmed); the helpers below convert to/from
physical units. Readback verification uses a tolerance sized to each
parameter's step grid, since the console quantizes what it stores.

Per "snapshot before touching anything": the channel's full EQ state is
read and returned before any write, and restore_console_eq() puts it
back verbatim. Committing EQ is a deliberate user action whose result is
*meant* to outlive the app, so this snapshot is NOT part of the crash
watchdog's automatic restore -- restoring it is equally deliberate.
"""
from __future__ import annotations

import math
import time
from typing import Any

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection

NUM_EQ_BANDS = 4

EQ_TYPE_PEQ = 2  # {LCut, LShv, PEQ, VEQ, HShv, HCut} -> parametric bell

FREQ_MIN_HZ, FREQ_MAX_HZ, FREQ_STEPS = 20.0, 20000.0, 201
GAIN_MIN_DB, GAIN_MAX_DB, GAIN_STEP_DB = -15.0, 15.0, 0.25
Q_MIN, Q_MAX, Q_STEPS = 10.0, 0.3, 72  # logf, inverted: normalized 0.0 = Q 10

# Readback tolerances: one step of each parameter's grid, with margin.
FREQ_NORM_TOLERANCE = 1.5 / (FREQ_STEPS - 1)
GAIN_NORM_TOLERANCE = 1.5 * GAIN_STEP_DB / (GAIN_MAX_DB - GAIN_MIN_DB)
Q_NORM_TOLERANCE = 1.5 / (Q_STEPS - 1)


class ChannelEqError(Exception):
    pass


def eq_on_addr(channel: int) -> str:
    return f"/ch/{channel:02d}/eq/on"


def eq_band_addr(channel: int, band: int, param: str) -> str:
    if not 1 <= band <= NUM_EQ_BANDS:
        raise ValueError(f"band must be 1-{NUM_EQ_BANDS}, got {band}")
    return f"/ch/{channel:02d}/eq/{band}/{param}"


# -- normalized-float conversions -------------------------------------------


def freq_to_normalized(hz: float) -> float:
    hz = min(max(hz, FREQ_MIN_HZ), FREQ_MAX_HZ)
    return math.log(hz / FREQ_MIN_HZ) / math.log(FREQ_MAX_HZ / FREQ_MIN_HZ)


def normalized_to_freq(x: float) -> float:
    return FREQ_MIN_HZ * (FREQ_MAX_HZ / FREQ_MIN_HZ) ** min(max(x, 0.0), 1.0)


def gain_to_normalized(db: float) -> float:
    db = min(max(db, GAIN_MIN_DB), GAIN_MAX_DB)
    return (db - GAIN_MIN_DB) / (GAIN_MAX_DB - GAIN_MIN_DB)


def normalized_to_gain(x: float) -> float:
    return GAIN_MIN_DB + (GAIN_MAX_DB - GAIN_MIN_DB) * min(max(x, 0.0), 1.0)


def q_to_normalized(q: float) -> float:
    # Inverted log scale: Q_MIN (10, widest normalized 0) down to Q_MAX (0.3).
    q = min(max(q, Q_MAX), Q_MIN)
    return math.log(q / Q_MIN) / math.log(Q_MAX / Q_MIN)


def normalized_to_q(x: float) -> float:
    return Q_MIN * (Q_MAX / Q_MIN) ** min(max(x, 0.0), 1.0)


# -- snapshot / commit / restore --------------------------------------------

_BAND_PARAMS = ("type", "f", "g", "q")


def _all_eq_addresses(channel: int) -> list[str]:
    addrs = [eq_on_addr(channel)]
    for band in range(1, NUM_EQ_BANDS + 1):
        addrs += [eq_band_addr(channel, band, p) for p in _BAND_PARAMS]
    return addrs


def snapshot_console_eq(
    osc: OscConnection, channel: int, correlation_id: str | None = None
) -> dict[str, Any]:
    """The channel's current console EQ state (on/off + all 4 bands' raw
    values), keyed by address. Values are opaque raw replies; an address
    whose query timed out maps to None (restore skips it)."""
    results = osc.query_many(_all_eq_addresses(channel), correlation_id=correlation_id)
    return {addr: (reply[0] if reply else None) for addr, reply in results.items()}


def commit_notches_to_console_eq(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    notches: list[dict],
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
) -> dict[str, Any]:
    """Write up to NUM_EQ_BANDS of the app's active notches into the
    channel's console EQ as parametric cuts, deepest first.

    notches is app.audio.filters.NotchFilterBank.active_notches() format:
    [{"frequency_hz": ..., "depth_db": ..., "q": ...}, ...]. Console EQ
    gain floors at -15 dB, so deeper app notches are clamped (and that
    clamping is reported in the returned summary -- a -18 dB app notch
    becomes a -15 dB console cut).

    Returns {"snapshot": <pre-write EQ state for restore_console_eq>,
    "written": [per-band summary]}. Raises ChannelEqError if any write
    fails its tolerance-checked readback -- after logging, with the
    snapshot embedded in the raised error's args untouched on the console
    side for whatever did land."""
    if not notches:
        raise ChannelEqError("no active notches to commit")
    correlation_id = correlation_id or diagnostics.new_correlation_id()

    snapshot = snapshot_console_eq(osc, channel, correlation_id=correlation_id)
    try:
        written = write_notches_to_console_eq(
            osc, diagnostics, channel, notches, correlation_id=correlation_id, pace_sec=pace_sec
        )
    except ChannelEqError as error:
        # Some writes may have landed before the failure -- expose the
        # pre-write snapshot on the error so the caller can still offer a
        # restore of whatever state the console is now in.
        error.snapshot = snapshot  # type: ignore[attr-defined]
        raise

    diagnostics.log_state_change(
        "console_eq_committed",
        before={"channel": channel, "snapshot": snapshot},
        after={"channel": channel, "written": written},
        correlation_id=correlation_id,
    )
    return {"snapshot": snapshot, "written": written}


def write_notches_to_console_eq(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    notches: list[dict],
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
) -> list[dict]:
    """Write up to NUM_EQ_BANDS of the app's active notches into the
    channel's console EQ as parametric cuts, deepest first, and switch EQ
    on. Unlike commit_notches_to_console_eq this does NOT snapshot first --
    the caller owns the snapshot (used by app.osc.console_eq_sync, which
    snapshots once when a channel enters internal-EQ mode and then writes
    repeatedly as feedback comes and goes). Returns the per-band summary;
    raises ChannelEqError on a failed tolerance-checked readback."""
    if not notches:
        raise ChannelEqError("no notches to write")
    correlation_id = correlation_id or diagnostics.new_correlation_id()

    chosen = sorted(notches, key=lambda n: n["depth_db"])[:NUM_EQ_BANDS]  # deepest (most negative) first
    chosen.sort(key=lambda n: n["frequency_hz"])  # bands laid out low->high like a human would

    written: list[dict] = []
    writes: list[tuple[str, Any, float | None]] = []  # (address, value, tolerance)
    for band, notch in enumerate(chosen, start=1):
        clamped_gain = max(notch["depth_db"], GAIN_MIN_DB)
        writes += [
            (eq_band_addr(channel, band, "type"), EQ_TYPE_PEQ, None),
            (eq_band_addr(channel, band, "f"), freq_to_normalized(notch["frequency_hz"]), FREQ_NORM_TOLERANCE),
            (eq_band_addr(channel, band, "g"), gain_to_normalized(clamped_gain), GAIN_NORM_TOLERANCE),
            (eq_band_addr(channel, band, "q"), q_to_normalized(notch["q"]), Q_NORM_TOLERANCE),
        ]
        written.append({
            "band": band,
            "frequency_hz": round(notch["frequency_hz"], 1),
            "gain_db": clamped_gain,
            "gain_clamped": clamped_gain != notch["depth_db"],
            "q": notch["q"],
        })
    writes.append((eq_on_addr(channel), 1, None))

    mismatches: list[str] = []
    for address, value, tolerance in writes:
        osc.send(address, value, correlation_id=correlation_id)
        time.sleep(pace_sec)
    for address, value, tolerance in writes:
        try:
            (actual,) = osc.query(address, correlation_id=correlation_id)
        except TimeoutError:
            mismatches.append(f"{address}: no readback reply")
            continue
        if tolerance is None:
            ok = actual == value
        else:
            ok = isinstance(actual, (int, float)) and abs(float(actual) - float(value)) <= tolerance
        if not ok:
            mismatches.append(f"{address}: wrote {value!r}, read back {actual!r}")

    if mismatches:
        error = ChannelEqError(
            f"write_notches_to_console_eq: {len(mismatches)} write(s) failed readback: {mismatches}"
        )
        diagnostics.log_error(error, context="write_notches_to_console_eq", correlation_id=correlation_id)
        raise error

    return written


def restore_console_eq(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channel: int,
    snapshot: dict[str, Any],
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
) -> None:
    """Write a snapshot_console_eq() capture back verbatim. Addresses whose
    snapshot value is None (their read timed out at snapshot time) are
    left untouched rather than guessed at."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    for address, value in snapshot.items():
        if value is None:
            continue
        osc.send(address, value, correlation_id=correlation_id)
        time.sleep(pace_sec)
    diagnostics.log_state_change(
        "console_eq_restored", after={"channel": channel}, correlation_id=correlation_id
    )
