from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.audio import devices as audio_devices
from app.midi import devices as midi_devices


def test_list_audio_devices_maps_sounddevice_query(monkeypatch):
    fake_sd = MagicMock()
    fake_sd.query_hostapis.return_value = [{"name": "ALSA"}, {"name": "OSS"}]
    fake_sd.query_devices.return_value = [
        {"name": "X-USB", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32, "default_samplerate": 48000.0},
        {"name": "Built-in Output", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100.0},
    ]
    monkeypatch.setattr(audio_devices, "sd", fake_sd)

    devices = audio_devices.list_audio_devices()
    assert len(devices) == 2
    assert devices[0].name == "X-USB"
    assert devices[0].host_api == "ALSA"
    assert devices[1].host_api == "OSS"


def test_list_input_and_output_devices_filter_by_channels(monkeypatch):
    fake_sd = MagicMock()
    fake_sd.query_hostapis.return_value = [{"name": "ALSA"}]
    fake_sd.query_devices.return_value = [
        {"name": "X-USB", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32, "default_samplerate": 48000.0},
        {"name": "Mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44100.0},
        {"name": "Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100.0},
    ]
    monkeypatch.setattr(audio_devices, "sd", fake_sd)

    inputs = audio_devices.list_input_devices()
    outputs = audio_devices.list_output_devices()
    assert {d.name for d in inputs} == {"X-USB", "Mic"}
    assert {d.name for d in outputs} == {"X-USB", "Speakers"}


def test_find_device_by_name(monkeypatch):
    fake_sd = MagicMock()
    fake_sd.query_hostapis.return_value = [{"name": "ALSA"}]
    fake_sd.query_devices.return_value = [
        {"name": "X-USB", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32, "default_samplerate": 48000.0},
    ]
    monkeypatch.setattr(audio_devices, "sd", fake_sd)

    found = audio_devices.find_device_by_name("X-USB")
    assert found is not None
    assert found.index == 0
    assert audio_devices.find_device_by_name("nonexistent") is None


def test_list_audio_devices_raises_clearly_when_portaudio_unavailable(monkeypatch):
    monkeypatch.setattr(audio_devices, "sd", None)
    monkeypatch.setattr(audio_devices, "_IMPORT_ERROR", OSError("PortAudio library not found"))

    with pytest.raises(RuntimeError, match="PortAudio is not available"):
        audio_devices.list_audio_devices()


def test_list_midi_ports_delegate_to_mido(monkeypatch):
    fake_mido = MagicMock()
    fake_mido.get_input_names.return_value = ["X-USB MIDI 1"]
    fake_mido.get_output_names.return_value = ["X-USB MIDI 1"]
    monkeypatch.setattr(midi_devices, "mido", fake_mido)

    assert midi_devices.list_midi_input_ports() == ["X-USB MIDI 1"]
    assert midi_devices.list_midi_output_ports() == ["X-USB MIDI 1"]
