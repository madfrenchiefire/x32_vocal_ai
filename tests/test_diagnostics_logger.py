from __future__ import annotations

import json

from app.diagnostics.logger import DiagnosticsLogger
from app.diagnostics.models import EventCategory


def test_log_writes_ring_buffer_and_file(tmp_path):
    logger = DiagnosticsLogger(log_dir=tmp_path, ring_buffer_size=10)
    event = logger.log(EventCategory.OSC_TX, {"address": "/xinfo", "args": []})

    assert event.seq == 1
    assert event.category == EventCategory.OSC_TX
    recent = logger.get_recent(1)
    assert recent[0]["seq"] == 1
    assert recent[0]["payload"]["address"] == "/xinfo"

    logger.close()
    lines = logger.session_file.read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["category"] == "osc_tx"
    assert on_disk["seq"] == 1


def test_ring_buffer_respects_max_size(tmp_path):
    logger = DiagnosticsLogger(log_dir=tmp_path, ring_buffer_size=3)
    for i in range(5):
        logger.log(EventCategory.STATE_CHANGE, {"i": i})
    ring = logger.get_all_ring()
    assert len(ring) == 3
    assert [e["payload"]["i"] for e in ring] == [2, 3, 4]
    logger.close()


def test_user_action_correlation_id_links_events(tmp_path):
    logger = DiagnosticsLogger(log_dir=tmp_path)
    correlation_id = logger.log_user_action("apply_routing", {"channels": [1, 2]})
    logger.log_osc_tx("/config/userrout/in/01", (5,), correlation_id=correlation_id)
    logger.log_osc_tx("/config/userrout/in/02", (6,), correlation_id=correlation_id)

    events = logger.get_recent(10)
    linked = [e for e in events if e["correlation_id"] == correlation_id]
    assert len(linked) == 3
    assert linked[0]["category"] == "user_action"
    logger.close()


def test_log_error_captures_traceback_and_state(tmp_path):
    def state_provider():
        return {"connection": {"connected": False}}

    logger = DiagnosticsLogger(log_dir=tmp_path, state_provider=state_provider)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        logger.log_error(exc, context="test_context")

    event = logger.get_recent(1)[0]
    assert event["category"] == "error"
    assert event["payload"]["error_type"] == "ValueError"
    assert "ValueError: boom" in event["payload"]["traceback"]
    assert event["payload"]["state_summary"] == {"connection": {"connected": False}}
    logger.close()


def test_summary_text_is_human_readable(tmp_path):
    logger = DiagnosticsLogger(log_dir=tmp_path)
    logger.log_user_action("ring_out_test")
    text = logger.summary_text(10)
    assert "user_action" in text
    assert "ring_out_test" in text
    logger.close()
