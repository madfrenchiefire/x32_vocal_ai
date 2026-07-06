from __future__ import annotations

import json

import pytest

from app.config import AppConfig, load_config, save_config


def test_load_config_defaults_when_missing(tmp_path):
    config = load_config(tmp_path / "does_not_exist.json")
    assert config == AppConfig()


def test_save_and_load_round_trip(tmp_path):
    config = AppConfig(console_ip="192.168.1.10", console_port=10023, midi_channel=3)
    path = save_config(config, tmp_path / "config.json")

    loaded = load_config(path)
    assert loaded.console_ip == "192.168.1.10"
    assert loaded.midi_channel == 3
    assert loaded.reconnect_backoff_sec == config.reconnect_backoff_sec


def test_unknown_key_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"not_a_real_field": 1}))
    with pytest.raises(ValueError):
        load_config(path)


def test_device_selection_defaults_to_unset(tmp_path):
    config = load_config(tmp_path / "does_not_exist.json")
    assert config.audio_input_device is None
    assert config.audio_output_device is None
    assert config.midi_input_port is None
    assert config.midi_output_port is None


def test_device_selection_round_trip(tmp_path):
    config = AppConfig(
        audio_input_device="X-USB",
        audio_output_device="X-USB",
        midi_input_port="X-USB MIDI 1",
        midi_output_port="X-USB MIDI 1",
    )
    path = save_config(config, tmp_path / "config.json")

    loaded = load_config(path)
    assert loaded.audio_input_device == "X-USB"
    assert loaded.audio_output_device == "X-USB"
    assert loaded.midi_input_port == "X-USB MIDI 1"
    assert loaded.midi_output_port == "X-USB MIDI 1"
