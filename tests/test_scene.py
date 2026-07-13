from __future__ import annotations

import pytest

from app.osc.connection import OscConnection
from app.osc.scene import SceneSaveError, save_console_scene


def _make_osc(fake_x32, diagnostics, app_state) -> OscConnection:
    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    return osc


def test_save_console_scene_sends_documented_form(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        save_console_scene(osc, diagnostics, 45, name="X32VOCAL SAFETY", note="pre-app")
    finally:
        osc.close()

    assert fake_x32.saved_scenes == [("scene", 45, "X32VOCAL SAFETY", "pre-app")]


def test_save_console_scene_default_note_has_timestamp(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        save_console_scene(osc, diagnostics, 99)
    finally:
        osc.close()

    (params,) = fake_x32.saved_scenes
    assert params[0] == "scene" and params[1] == 99
    assert "pre-app state" in params[3]


def test_save_console_scene_rejects_out_of_range_slot(fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    try:
        with pytest.raises(ValueError):
            save_console_scene(osc, diagnostics, 100)
        with pytest.raises(ValueError):
            save_console_scene(osc, diagnostics, -1)
    finally:
        osc.close()


def test_save_console_scene_raises_on_failure_status(monkeypatch, fake_x32, diagnostics, app_state):
    osc = _make_osc(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(osc, "query", lambda *a, **k: ("scene", 0))
    try:
        with pytest.raises(SceneSaveError, match="failure"):
            save_console_scene(osc, diagnostics, 45)
    finally:
        osc.close()


def test_save_console_scene_raises_on_timeout(monkeypatch, fake_x32, diagnostics, app_state):
    def timeout(*a, **k):
        raise TimeoutError("no reply")
    osc = _make_osc(fake_x32, diagnostics, app_state)
    monkeypatch.setattr(osc, "query", timeout)
    try:
        with pytest.raises(SceneSaveError, match="acknowledge"):
            save_console_scene(osc, diagnostics, 45)
    finally:
        osc.close()
