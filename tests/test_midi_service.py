from __future__ import annotations

from unittest.mock import MagicMock

import mido
import pytest

import app.midi.service as service_module
from app.config import AppConfig
from app.midi.service import MidiService
from app.midi.slots import SlotsFullError


def _make_service(diagnostics, app_state, **config_overrides) -> MidiService:
    config = AppConfig(midi_channel=16, **config_overrides)
    return MidiService(config=config, diagnostics=diagnostics, state=app_state, osc=None)


def test_select_channel_assigns_slot_and_updates_state(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    assignment = service.select_channel(9)
    assert assignment.slot == 1
    assert assignment.set_name == "A"
    assert app_state.channels[9].midi_slot == 1


def test_select_channel_when_full_without_hard_cap_is_app_controlled_only(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    for ch in range(1, 9):
        service.select_channel(ch)

    result = service.select_channel(20)
    assert result is None
    assert app_state.channels[20].midi_slot is None


def test_select_channel_when_full_with_hard_cap_raises(diagnostics, app_state):
    service = _make_service(diagnostics, app_state, midi_hard_cap_at_8_channels=True)
    for ch in range(1, 9):
        service.select_channel(ch)

    with pytest.raises(SlotsFullError):
        service.select_channel(20)


def test_deselect_channel_releases_slot(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)
    service.deselect_channel(9)
    assert app_state.channels[9].midi_slot is None
    assert service.slots.slot_for_channel(9) is None


def test_dispatch_cc_sensitivity_calls_hook(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)  # slot 1 -> sensitivity CC 11
    calls = []
    service.on_sensitivity_change = lambda ch, value: calls.append((ch, value))

    service._dispatch_cc(control=11, value=64)
    assert calls == [(9, pytest.approx(64 / 127.0))]


def test_dispatch_cc_ai_toggle_only_fires_on_press(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)  # slot 1 -> AI toggle CC 1
    calls = []
    service.on_ai_toggle = lambda ch, enabled: calls.append((ch, enabled))

    service._dispatch_cc(control=1, value=0)  # release -- no action
    assert calls == []

    service._dispatch_cc(control=1, value=127)  # press -- toggles
    assert calls == [(9, True)]


def test_dispatch_cc_insert_bypass_only_fires_on_press(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)  # slot 1 -> insert/bypass CC 21
    calls = []
    service.on_insert_bypass_toggle = lambda ch: calls.append(ch)

    service._dispatch_cc(control=21, value=0)
    assert calls == []
    service._dispatch_cc(control=21, value=127)
    assert calls == [9]


def test_on_midi_message_logs_and_dispatches_for_configured_channel(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)
    calls = []
    service.on_ai_toggle = lambda ch, enabled: calls.append((ch, enabled))

    # mido channels are 0-indexed; config.midi_channel=16 -> mido channel 15.
    message = mido.Message("control_change", channel=15, control=1, value=127)
    service._on_midi_message(message)

    assert calls == [(9, True)]
    events = diagnostics.get_recent(5)
    midi_events = [e for e in events if e["category"] == "midi_rx"]
    assert len(midi_events) == 1
    assert midi_events[0]["payload"]["parsed"]["control"] == 1


def test_on_midi_message_ignores_wrong_midi_channel(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)
    calls = []
    service.on_ai_toggle = lambda ch, enabled: calls.append((ch, enabled))

    message = mido.Message("control_change", channel=0, control=1, value=127)  # wrong channel
    service._on_midi_message(message)
    assert calls == []


def test_on_midi_message_ignores_non_control_change(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    service.select_channel(9)
    calls = []
    service.on_ai_toggle = lambda ch, enabled: calls.append((ch, enabled))

    message = mido.Message("note_on", channel=15, note=60, velocity=127)
    service._on_midi_message(message)  # should log and return, not dispatch
    assert calls == []
    events = diagnostics.get_recent(5)
    assert any(e["category"] == "midi_rx" for e in events)


def test_start_opens_configured_ports_and_stop_closes_them(monkeypatch, diagnostics, app_state):
    fake_input = MagicMock()
    fake_output = MagicMock()
    open_input = MagicMock(return_value=fake_input)
    open_output = MagicMock(return_value=fake_output)
    monkeypatch.setattr(service_module.mido, "open_input", open_input)
    monkeypatch.setattr(service_module.mido, "open_output", open_output)

    service = _make_service(diagnostics, app_state, midi_input_port="X-USB MIDI 1", midi_output_port="X-USB MIDI 1")
    service.start()

    open_input.assert_called_once()
    assert open_input.call_args.args[0] == "X-USB MIDI 1"
    assert open_input.call_args.kwargs["callback"] == service._on_midi_message
    open_output.assert_called_once_with("X-USB MIDI 1")

    service.stop()
    fake_input.close.assert_called_once()
    fake_output.close.assert_called_once()


def test_start_without_configured_port_raises(diagnostics, app_state):
    service = _make_service(diagnostics, app_state)
    with pytest.raises(ValueError):
        service.start()


# -- console-side provisioning (assign-set writes) ---------------------------


def _make_osc_service(fake_x32, diagnostics, app_state) -> MidiService:
    from app.osc.connection import OscConnection

    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    config = AppConfig(midi_channel=16)
    return MidiService(config=config, diagnostics=diagnostics, state=app_state, osc=osc)


def test_select_channel_provisions_console_controls(fake_x32, diagnostics, app_state):
    service = _make_osc_service(fake_x32, diagnostics, app_state)
    try:
        assignment = service.select_channel(9)  # slot 1 -> Set A, index 1
        assert assignment.slot == 1
        # Sensitivity encoder = CC 11, AI button = CC 1, insert/bypass = CC 21,
        # all on the configured MIDI channel 16, buttons as "Midi Push" (MC).
        assert fake_x32.extra_responses["/config/userctrl/A/enc/1"] == ("MC16011",)
        assert fake_x32.extra_responses["/config/userctrl/A/btn/5"] == ("MC16001",)
        assert fake_x32.extra_responses["/config/userctrl/A/btn/9"] == ("MC16021",)
    finally:
        service.osc.close()


def test_select_channel_slot_5_provisions_set_b(fake_x32, diagnostics, app_state):
    service = _make_osc_service(fake_x32, diagnostics, app_state)
    try:
        for ch in range(1, 6):
            service.select_channel(ch)  # channel 5 lands in slot 5 -> Set B, index 1
        assert fake_x32.extra_responses["/config/userctrl/B/enc/1"] == ("MC16015",)
        assert fake_x32.extra_responses["/config/userctrl/B/btn/5"] == ("MC16005",)
        assert fake_x32.extra_responses["/config/userctrl/B/btn/9"] == ("MC16025",)
    finally:
        service.osc.close()


def test_provisioning_failure_is_nonfatal(monkeypatch, fake_x32, diagnostics, app_state):
    service = _make_osc_service(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(service.osc, "send", lambda *a, **k: None)  # writes never land
    try:
        assignment = service.select_channel(9)
        # Selection still succeeds; the failure is logged, not raised.
        assert assignment is not None
        assert app_state.channels[9].midi_slot == 1
        events = diagnostics.get_recent(10)
        assert any(e["category"] == "error" for e in events)
    finally:
        service.osc.close()


def test_deselect_channel_restores_snapshot_values(fake_x32, diagnostics, app_state):
    app_state.set_assign_set_snapshot({
        "/config/userctrl/A/enc/1": ("S0000",),
        "/config/userctrl/A/btn/5": ("Mc00000",),
        "/config/userctrl/A/btn/9": None,  # never answered at snapshot time -- left alone
    })
    service = _make_osc_service(fake_x32, diagnostics, app_state)
    try:
        service.select_channel(9)
        assert fake_x32.extra_responses["/config/userctrl/A/enc/1"] == ("MC16011",)

        service.deselect_channel(9)
        assert fake_x32.extra_responses["/config/userctrl/A/enc/1"] == ("S0000",)
        assert fake_x32.extra_responses["/config/userctrl/A/btn/5"] == ("Mc00000",)
        # No snapshot value -> untouched (still holding the provisioned value).
        assert fake_x32.extra_responses["/config/userctrl/A/btn/9"] == ("MC16021",)
    finally:
        service.osc.close()
