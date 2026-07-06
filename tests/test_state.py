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


def test_summary_is_a_plain_dict_copy():
    state = AppState()
    state.update_connection(connected=True)
    summary = state.summary()
    assert summary["connection"]["connected"] is True
    assert len(summary["channels"]) == 32

    # Mutating the returned summary must not affect internal state.
    summary["connection"]["connected"] = False
    assert state.connection.connected is True
