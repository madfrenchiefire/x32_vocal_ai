from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app.main as main_module
from app.config import AppConfig, save_config
from app.osc.connection import OscConnectionError
from app.osc.routing_apply import RoutingApplyError


def _write_config(tmp_path, **overrides) -> str:
    config_path = tmp_path / "config.json"
    config = AppConfig(log_dir=str(tmp_path / "logs"), snapshot_dir=str(tmp_path / "snapshots"), **overrides)
    save_config(config, config_path)
    return str(config_path)


def test_main_with_nothing_configured_starts_only_the_web_ui(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path)
    run_web = MagicMock()
    monkeypatch.setattr(main_module, "run_web", run_web)

    exit_code = main_module.main(["--config", config_path])

    assert exit_code == 0
    run_web.assert_called_once()
    assert run_web.call_args.kwargs["osc"] is None
    assert run_web.call_args.kwargs["audio_engine"] is None
    assert run_web.call_args.kwargs["midi_service"] is None


def test_main_arms_watchdog_even_with_no_console_configured(monkeypatch, tmp_path):
    # The watchdog must exist from the start (with osc=None) so
    # /api/console/connect has something to wire a live connection into
    # later -- it shouldn't only appear once a console happens to connect
    # at startup.
    config_path = _write_config(tmp_path)
    run_web = MagicMock()
    monkeypatch.setattr(main_module, "run_web", run_web)

    watchdog_instance = MagicMock()
    monkeypatch.setattr(main_module, "Watchdog", MagicMock(return_value=watchdog_instance))

    exit_code = main_module.main(["--config", config_path])

    assert exit_code == 0
    watchdog_instance.start.assert_called_once()
    assert run_web.call_args.kwargs["watchdog"] is watchdog_instance
    watchdog_instance.trigger_full_restore.assert_called_once_with(reason="clean_shutdown")
    watchdog_instance.stop.assert_called_once()


def test_main_starts_osc_and_arms_watchdog_when_console_connects(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path, console_ip="10.10.0.142")
    run_web = MagicMock()
    monkeypatch.setattr(main_module, "run_web", run_web)

    fake_osc = MagicMock()
    fake_osc.xinfo = {"model": "X32"}
    monkeypatch.setattr(main_module, "OscConnection", MagicMock(return_value=fake_osc))

    watchdog_instance = MagicMock()
    watchdog_class = MagicMock(return_value=watchdog_instance)
    monkeypatch.setattr(main_module, "Watchdog", watchdog_class)

    exit_code = main_module.main(["--config", config_path])

    assert exit_code == 0
    fake_osc.connect.assert_called_once()
    assert run_web.call_args.kwargs["osc"] is fake_osc
    watchdog_instance.start.assert_called_once()
    watchdog_instance.trigger_full_restore.assert_called_once_with(reason="clean_shutdown")
    watchdog_instance.stop.assert_called_once()
    fake_osc.close.assert_called_once()


def test_main_continues_when_console_connect_fails(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path, console_ip="10.10.0.142")
    run_web = MagicMock()
    monkeypatch.setattr(main_module, "run_web", run_web)

    fake_osc = MagicMock()
    fake_osc.connect.side_effect = OscConnectionError("no reply")
    monkeypatch.setattr(main_module, "OscConnection", MagicMock(return_value=fake_osc))

    exit_code = main_module.main(["--config", config_path])

    assert exit_code == 0
    assert run_web.call_args.kwargs["osc"] is None


def test_main_starts_midi_and_wires_hooks(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path, midi_input_port="X-USB MIDI 1", midi_output_port="X-USB MIDI 1")
    run_web = MagicMock()
    monkeypatch.setattr(main_module, "run_web", run_web)

    fake_midi = MagicMock()
    monkeypatch.setattr(main_module, "MidiService", MagicMock(return_value=fake_midi))

    exit_code = main_module.main(["--config", config_path])

    assert exit_code == 0
    fake_midi.start.assert_called_once()
    assert run_web.call_args.kwargs["midi_service"] is fake_midi
    fake_midi.stop.assert_called_once()
    assert callable(fake_midi.on_ai_toggle)
    assert callable(fake_midi.on_sensitivity_change)
    assert callable(fake_midi.on_insert_bypass_toggle)


def test_main_starts_audio_engine_with_all_32_card_slots(monkeypatch, tmp_path):
    config_path = _write_config(tmp_path, audio_input_device="Interface In", audio_output_device="Interface Out")
    run_web = MagicMock()
    monkeypatch.setattr(main_module, "run_web", run_web)

    captured = {}

    class FakeAudioEngine:
        def __init__(self, config, diagnostics, filter_banks=None, echo_cancellers=None, state=None):
            captured["filter_banks"] = filter_banks
            captured["echo_cancellers"] = echo_cancellers
            self.stop = MagicMock()

        def start(self):
            pass

    monkeypatch.setattr(main_module, "AudioEngine", FakeAudioEngine)

    exit_code = main_module.main(["--config", config_path])

    assert exit_code == 0
    assert len(captured["filter_banks"]) == 32
    assert captured["echo_cancellers"] == {}  # echo cancellation off by default
    assert run_web.call_args.kwargs["audio_engine"] is not None


def test_on_insert_bypass_toggle_logs_error_without_connection_or_snapshot(diagnostics, app_state):
    # Exercise the hook builder directly rather than through main(): call
    # _start_midi with osc=None and inspect the wired hook.
    fake_midi = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main_module, "MidiService", MagicMock(return_value=fake_midi))
        config = AppConfig(midi_input_port="X-USB MIDI 1")
        main_module._start_midi(config, diagnostics, app_state, osc=None)

    on_insert_bypass_toggle = fake_midi.on_insert_bypass_toggle
    on_insert_bypass_toggle(1)  # no osc, no snapshot -- must not raise

    events = [e for e in diagnostics.get_recent(5) if e["category"] == "error"]
    assert any(e["payload"]["context"] == "midi_insert_bypass_toggle" for e in events)


def test_on_insert_bypass_toggle_calls_bypass_channel_when_ready(monkeypatch, diagnostics, app_state):
    fake_midi = MagicMock()
    fake_osc = MagicMock()
    monkeypatch.setattr(main_module, "MidiService", MagicMock(return_value=fake_midi))
    bypass_channel_mock = MagicMock(return_value=True)
    monkeypatch.setattr(main_module, "bypass_channel", bypass_channel_mock)

    config = AppConfig(midi_input_port="X-USB MIDI 1")
    app_state.set_snapshot(MagicMock())
    main_module._start_midi(config, diagnostics, app_state, osc=fake_osc)

    fake_midi.on_insert_bypass_toggle(1)
    bypass_channel_mock.assert_called_once()
    assert bypass_channel_mock.call_args.args[0] is fake_osc
    assert bypass_channel_mock.call_args.args[2] == 1


def test_on_insert_bypass_toggle_swallows_routing_apply_error(monkeypatch, diagnostics, app_state):
    fake_midi = MagicMock()
    fake_osc = MagicMock()
    monkeypatch.setattr(main_module, "MidiService", MagicMock(return_value=fake_midi))
    monkeypatch.setattr(main_module, "bypass_channel", MagicMock(side_effect=RoutingApplyError("boom")))

    config = AppConfig(midi_input_port="X-USB MIDI 1")
    app_state.set_snapshot(MagicMock())
    main_module._start_midi(config, diagnostics, app_state, osc=fake_osc)

    fake_midi.on_insert_bypass_toggle(1)  # must not raise
    events = [e for e in diagnostics.get_recent(5) if e["category"] == "error"]
    assert any(e["payload"]["context"] == "midi_insert_bypass_toggle" for e in events)
