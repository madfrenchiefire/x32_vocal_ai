"""Panic: instantly mute every app-managed channel.

The gig-safe last resort for a runaway squeal: one click writes
`/ch/NN/mix/on 0` (doc-confirmed enum {OFF, ON}) to every channel the app
currently manages (i.e. holds a Card slot this session). Un-panic writes
back the *snapshotted* pre-panic mute states -- a channel the engineer
already had muted stays muted, rather than being blindly switched on.

Ordering matters here more than anywhere else in the app: all mute writes
go out first, unpaced (a few UDP datagrams -- silencing the PA is the
whole point and every millisecond counts), and only then are they
readback-verified best-effort.
"""
from __future__ import annotations

from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection

MUTED, UNMUTED = 0, 1


class PanicError(Exception):
    pass


def channel_mix_on_addr(channel: int) -> str:
    return f"/ch/{channel:02d}/mix/on"


def panic_mute(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    channels: list[int],
    correlation_id: str | None = None,
) -> dict[int, int | None]:
    """Mute every given channel NOW. Returns {channel: pre-panic mix/on
    value or None if it couldn't be read} for panic_restore. Snapshot
    reads happen before the writes but are not allowed to block them:
    a channel whose read times out is still muted -- it just restores to
    unmuted-unknown (None -> skipped) later."""
    if not channels:
        raise PanicError("no app-managed channels to mute")
    correlation_id = correlation_id or diagnostics.new_correlation_id()

    snapshot_raw = osc.query_many(
        [channel_mix_on_addr(ch) for ch in channels], timeout=0.5, retries=0,
        correlation_id=correlation_id,
    )
    snapshot = {
        ch: (snapshot_raw[channel_mix_on_addr(ch)][0] if snapshot_raw[channel_mix_on_addr(ch)] else None)
        for ch in channels
    }

    # The point of no return: fire every mute, no pacing, no waiting.
    for ch in channels:
        osc.send(channel_mix_on_addr(ch), MUTED, correlation_id=correlation_id)

    mismatches = []
    for ch in channels:
        actual = osc.query_until_match(channel_mix_on_addr(ch), MUTED, correlation_id=correlation_id)
        if actual != MUTED:
            mismatches.append(ch)

    diagnostics.log_state_change(
        "panic_muted",
        before={"mix_on": {str(k): v for k, v in snapshot.items()}},
        after={"channels": channels, "unconfirmed": mismatches},
        correlation_id=correlation_id,
    )
    if mismatches:
        diagnostics.log_error(
            PanicError(f"panic mute unconfirmed for channel(s) {mismatches}"),
            context="panic_mute", correlation_id=correlation_id,
        )
    return snapshot


def panic_restore(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    snapshot: dict[int, int | None],
    correlation_id: str | None = None,
) -> None:
    """Write back the pre-panic mute states. A channel whose pre-panic
    state couldn't be read (None) is left muted rather than guessed at --
    un-muting a channel the engineer had muted is worse than the reverse."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()
    for ch, value in snapshot.items():
        if value is None:
            continue
        osc.send(channel_mix_on_addr(ch), value, correlation_id=correlation_id)
    diagnostics.log_state_change(
        "panic_restored",
        after={"mix_on": {str(k): v for k, v in snapshot.items()}},
        correlation_id=correlation_id,
    )
