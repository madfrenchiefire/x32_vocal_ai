from __future__ import annotations

from app.audio.devices import AudioDevice
from app.tools import list_devices


def _patch_all(monkeypatch, *, audio_in_error=None, midi_in_error=None):
    def fake_audio_in(asio_only=True):
        if audio_in_error:
            raise audio_in_error
        return [AudioDevice(0, "X-USB", "ALSA", 32, 0, 48000.0)]

    def fake_audio_out(asio_only=True):
        return [AudioDevice(0, "X-USB", "ALSA", 0, 32, 48000.0)]

    def fake_midi_in():
        if midi_in_error:
            raise midi_in_error
        return ["X-USB MIDI 1"]

    def fake_midi_out():
        return ["X-USB MIDI 1"]

    monkeypatch.setattr(list_devices, "list_input_devices", fake_audio_in)
    monkeypatch.setattr(list_devices, "list_output_devices", fake_audio_out)
    monkeypatch.setattr(list_devices, "list_midi_input_ports", fake_midi_in)
    monkeypatch.setattr(list_devices, "list_midi_output_ports", fake_midi_out)


def test_list_devices_json_success(monkeypatch, tmp_path, capsys):
    _patch_all(monkeypatch)
    rc = list_devices.main(["--json", "--log-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "X-USB" in out
    assert "X-USB MIDI 1" in out


def test_list_devices_one_subsystem_failing_does_not_blank_the_others(monkeypatch, tmp_path, capsys):
    _patch_all(monkeypatch, midi_in_error=RuntimeError("no ALSA sequencer"))
    rc = list_devices.main(["--json", "--log-dir", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert '"audio_inputs": [' in out
    assert "X-USB" in out
    assert "no ALSA sequencer" in out


def test_list_devices_table_output_reports_error_per_section(monkeypatch, tmp_path, capsys):
    _patch_all(monkeypatch, audio_in_error=RuntimeError("PortAudio is not available"))
    rc = list_devices.main(["--log-dir", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Audio input devices:" in out
    assert "ERROR: PortAudio is not available" in out
    # Audio outputs should still be listed even though inputs failed.
    assert "X-USB" in out
