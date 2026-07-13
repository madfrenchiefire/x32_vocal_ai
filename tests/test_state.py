from __future__ import annotations

import pytest

from app.state import AppState


def test_update_connection_sets_fields():
    state = AppState()
    state.update_connection(connected=True, host="192.168.1.10", firmware_version="4.06")
    assert state.connection.connected is True
    assert state.connection.host == "192.168.1.10"
    assert state.connection.firmware_version == "4.06"


def test_update_connection_rejects_unknown_field():
    state = AppState()
    with pytest.raises(AttributeError):
        state.update_connection(not_a_field=True)


def test_set_snapshot_tracks_history():
    state = AppState()
    state.set_snapshot({"name": "a"}, saved_path="/tmp/a.json")
    state.set_snapshot({"name": "b"}, saved_path="/tmp/b.json")
    assert state.current_snapshot == {"name": "b"}
    assert state.snapshot_history == ["/tmp/a.json", "/tmp/b.json"]


def test_set_assign_set_snapshot():
    state = AppState()
    assert state.assign_set_snapshot is None
    state.set_assign_set_snapshot({"/config/userctrl/A/enc/1": (1,)})
    assert state.assign_set_snapshot == {"/config/userctrl/A/enc/1": (1,)}
    assert state.summary()["has_assign_set_snapshot"] is True


def test_apply_channel_configs_sets_name_and_color():
    state = AppState()
    state.apply_channel_configs({1: ("Ruby Vocal", "YE"), 9: ("Left Vocal", "RD")})
    assert state.channels[1].scribble_name == "Ruby Vocal"
    assert state.channels[1].scribble_color == "YE"
    assert state.channels[9].scribble_name == "Left Vocal"
    assert state.channels[9].scribble_color == "RD"


def test_apply_channel_configs_leaves_unresolved_fields_untouched():
    state = AppState()
    state.apply_channel_configs({1: ("Ruby Vocal", "YE")})
    # A re-query where channel 1's reads timed out entirely and channel
    # 2's name arrived but its color didn't.
    state.apply_channel_configs({1: (None, None), 2: ("Drums", None)})
    assert state.channels[1].scribble_name == "Ruby Vocal"  # not blanked out
    assert state.channels[1].scribble_color == "YE"
    assert state.channels[2].scribble_name == "Drums"
    assert state.channels[2].scribble_color is None


def test_summary_is_a_plain_dict_copy():
    state = AppState()
    state.update_connection(connected=True)
    summary = state.summary()
    assert summary["connection"]["connected"] is True
    assert len(summary["channels"]) == 32

    # Mutating the returned summary must not affect internal state.
    summary["connection"]["connected"] = False
    assert state.connection.connected is True
