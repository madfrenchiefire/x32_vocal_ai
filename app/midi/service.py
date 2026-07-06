"""MIDI control surface service -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's "MIDI service" section: rides the X-USB card (mido +
python-rtmidi on a background thread, fully isolated from the audio
callback). One dedicated MIDI channel (default from AppConfig.midi_channel).
Buttons = CC toggle (127/0), encoders = absolute CC 0-127. Provisions
console-side assign-sets A/B via app.osc (once verified -- see
app.osc.addresses) to mirror each slot's controls on the X32 surface.
"""
from __future__ import annotations

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import OscConnection
from app.state import AppState


class MidiService:
    def __init__(
        self,
        config: AppConfig,
        diagnostics: DiagnosticsLogger,
        state: AppState,
        osc: OscConnection | None = None,
    ) -> None:
        raise NotImplementedError("MIDI service is implemented in a later phase")

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def select_channel(self, channel: int) -> None:
        """Grab the lowest free slot and provision its controls via OSC."""
        raise NotImplementedError

    def deselect_channel(self, channel: int) -> None:
        raise NotImplementedError

    def _on_midi_message(self, raw_bytes: bytes) -> None:
        """Callback for incoming MIDI (mido input port), logged as
        EventCategory.MIDI_RX via DiagnosticsLogger before any dispatch."""
        raise NotImplementedError
