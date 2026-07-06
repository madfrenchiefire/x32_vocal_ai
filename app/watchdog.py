"""Crash watchdog -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's core design principle #3 ("Gig-safe defaults"): any
failure mode must resolve to "mics on their original patch, console
behaving stock" within ~1 second. On detecting a crash/hang of the
audio/OSC/MIDI services, this replays the most recent routing snapshot via
app.osc.routing_apply.restore_snapshot (not yet implemented) and resets
scribble-strip state.
"""
from __future__ import annotations

from app.diagnostics.logger import DiagnosticsLogger
from app.state import AppState


class Watchdog:
    def __init__(self, state: AppState, diagnostics: DiagnosticsLogger) -> None:
        raise NotImplementedError("crash watchdog is implemented in a later phase")

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def trigger_full_restore(self, reason: str) -> None:
        raise NotImplementedError
