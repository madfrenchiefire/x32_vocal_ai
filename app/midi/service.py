"""MIDI control surface service.

Rides the X-USB card (mido + python-rtmidi on a background thread -- mido's
callback runs on its own backend-managed thread, fully isolated from the
audio callback). One dedicated MIDI channel (default 16, configurable).
Buttons = CC toggle (127 = pressed, 0 = released, only 127 triggers an
action); encoders = absolute CC 0-127, scaled to 0.0-1.0 for sensitivity.

Provisioning a slot's hardware controls on the console (assign-set writes)
is NOT functional yet -- app.osc.assign_set needs a confirmed MIDI-
assignment value format first (see that module's docstring). The slot
lifecycle and CC dispatch below are complete and independently testable;
_provision_slot is the one clearly marked no-op pending that.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import mido

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.midi.slots import SlotAssignment, SlotManager, SlotsFullError, ai_toggle_cc, insert_bypass_cc, sensitivity_cc
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
        self.config = config
        self.diagnostics = diagnostics
        self.state = state
        self.osc = osc
        self.slots = SlotManager()

        self._input_port: mido.ports.BaseInput | None = None
        self._output_port: mido.ports.BaseOutput | None = None
        self._running = threading.Event()

        # Injectable hooks for what a control actually does -- kept here
        # rather than importing the audio engine directly, so MidiService
        # doesn't need to know about NotchFilterBank/FeedbackDetector.
        self.on_sensitivity_change: Callable[[int, float], None] | None = None
        self.on_ai_toggle: Callable[[int, bool], None] | None = None
        self.on_insert_bypass_toggle: Callable[[int], None] | None = None

    def start(self) -> None:
        if self.config.midi_input_port is None:
            raise ValueError("midi_input_port not configured -- see app.midi.devices.list_midi_input_ports()")
        self._input_port = mido.open_input(self.config.midi_input_port, callback=self._on_midi_message)
        if self.config.midi_output_port is not None:
            self._output_port = mido.open_output(self.config.midi_output_port)
        self._running.set()
        correlation_id = self.diagnostics.log_user_action(
            "midi_service_started",
            {"input_port": self.config.midi_input_port, "output_port": self.config.midi_output_port},
        )
        self.diagnostics.log_state_change("midi_service_started", correlation_id=correlation_id)

    def stop(self) -> None:
        self._running.clear()
        if self._input_port is not None:
            self._input_port.close()
            self._input_port = None
        if self._output_port is not None:
            self._output_port.close()
            self._output_port = None

    def select_channel(self, channel: int, correlation_id: str | None = None) -> SlotAssignment | None:
        """Grab the lowest free slot and provision its controls via OSC. If
        all 8 slots are full, per CLAUDE.md this channel can still be
        processed (audio-only, app-controlled) unless
        AppConfig.midi_hard_cap_at_8_channels is set."""
        correlation_id = correlation_id or self.diagnostics.new_correlation_id()
        try:
            assignment = self.slots.assign(channel)
        except SlotsFullError:
            if self.config.midi_hard_cap_at_8_channels:
                raise
            self.state.channels[channel].midi_slot = None
            self.diagnostics.log_state_change(
                "midi_slot_unavailable_channel_app_controlled_only",
                after={"channel": channel},
                correlation_id=correlation_id,
            )
            return None

        self.state.channels[channel].midi_slot = assignment.slot
        if self.osc is not None:
            self._provision_slot(assignment, correlation_id)
        self.diagnostics.log_state_change(
            "midi_slot_assigned",
            after={"channel": channel, "slot": assignment.slot, "set": assignment.set_name},
            correlation_id=correlation_id,
        )
        return assignment

    def deselect_channel(self, channel: int, correlation_id: str | None = None) -> None:
        correlation_id = correlation_id or self.diagnostics.new_correlation_id()
        self.slots.release(channel)
        self.state.channels[channel].midi_slot = None
        self.diagnostics.log_state_change(
            "midi_slot_released", after={"channel": channel}, correlation_id=correlation_id
        )

    def _provision_slot(self, assignment: SlotAssignment, correlation_id: str) -> None:
        """TODO-VERIFY: wire this slot's encoder + 2 buttons to `assignment.channel`
        via app.osc.assign_set.write_assignment() once the MIDI-assignment
        value format is confirmed (see that module's docstring). Left as a
        deliberate no-op call site until then."""
        return

    def _on_midi_message(self, message: mido.Message) -> None:
        """mido's backend callback thread -- keep this fast and non-blocking."""
        raw_bytes = bytes(message.bytes())
        parsed: dict = {"type": message.type, "channel": getattr(message, "channel", None)}
        if message.type == "control_change":
            parsed["control"] = message.control
            parsed["value"] = message.value
        self.diagnostics.log_midi_rx(raw_bytes, parsed)

        if message.type != "control_change":
            return
        if getattr(message, "channel", None) != self.config.midi_channel - 1:  # mido channels are 0-indexed
            return
        self._dispatch_cc(message.control, message.value)

    def _dispatch_cc(self, control: int, value: int) -> None:
        for slot in range(1, 9):
            channel = self.slots.channel_for_slot(slot)
            if channel is None:
                continue
            if control == sensitivity_cc(slot):
                if self.on_sensitivity_change is not None:
                    self.on_sensitivity_change(channel, value / 127.0)
            elif control == ai_toggle_cc(slot):
                if value == 127 and self.on_ai_toggle is not None:
                    self.on_ai_toggle(channel, not self.state.channels[channel].ai_enabled)
            elif control == insert_bypass_cc(slot):
                if value == 127 and self.on_insert_bypass_toggle is not None:
                    self.on_insert_bypass_toggle(channel)
