from __future__ import annotations

import signal
import sys
from unittest.mock import MagicMock

from app.osc import addresses
from app.osc.connection import OscConnection
from app.osc.routing_snapshot import RoutingSnapshot
from app.watchdog import Watchdog


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


def _make_snapshot() -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        created_at="2026-01-01T00:00:00.000Z",
        name="watchdog_test",
        console={"model": "X32"},
        userrout_in=[0] * addresses.NUM_USERROUT_IN,
        userrout_out=[0] * addresses.NUM_USERROUT_OUT,
        routing={},
    )


def _make_watchdog(diagnostics, app_state, snapshot=None, restore_fn=None) -> Watchdog:
    osc = MagicMock(spec=OscConnection)
    restore_fn = restore_fn if restore_fn is not None else MagicMock(return_value=[])
    return Watchdog(
        osc=osc,
        diagnostics=diagnostics,
        state=app_state,
        snapshot_provider=lambda: snapshot,
        restore_fn=restore_fn,
    )


def test_trigger_full_restore_calls_restore_fn_once(diagnostics, app_state):
    snapshot = _make_snapshot()
    restore_fn = MagicMock(return_value=[])
    watchdog = _make_watchdog(diagnostics, app_state, snapshot=snapshot, restore_fn=restore_fn)

    mismatches = watchdog.trigger_full_restore(reason="test")
    assert mismatches == []
    restore_fn.assert_called_once()
    assert restore_fn.call_args.args[0] is watchdog.osc
    assert restore_fn.call_args.args[2] is snapshot

    # Idempotent: a second trigger within the same armed session must not
    # replay the snapshot again (mirrors a signal handler followed by the
    # atexit hook it also fires).
    watchdog.trigger_full_restore(reason="test-again")
    restore_fn.assert_called_once()


def test_trigger_full_restore_skips_when_no_console_connection(diagnostics, app_state):
    snapshot = _make_snapshot()
    restore_fn = MagicMock(return_value=[])
    watchdog = Watchdog(
        osc=None, diagnostics=diagnostics, state=app_state, snapshot_provider=lambda: snapshot, restore_fn=restore_fn
    )

    mismatches = watchdog.trigger_full_restore(reason="test")
    assert mismatches == []
    restore_fn.assert_not_called()

    events = diagnostics.get_recent(5)
    assert any(
        e["payload"]["event"] == "restore_skipped_no_console_connection"
        for e in events if e["category"] == "watchdog"
    )


def test_trigger_full_restore_skips_when_no_snapshot(diagnostics, app_state):
    restore_fn = MagicMock(return_value=[])
    watchdog = _make_watchdog(diagnostics, app_state, snapshot=None, restore_fn=restore_fn)

    mismatches = watchdog.trigger_full_restore(reason="test")
    assert mismatches == []
    restore_fn.assert_not_called()

    events = diagnostics.get_recent(5)
    assert any(e["payload"]["event"] == "restore_skipped_no_snapshot" for e in events if e["category"] == "watchdog")


def test_trigger_full_restore_also_restores_assign_sets(diagnostics, app_state):
    routing_snapshot = _make_snapshot()
    assign_set_snapshot = {"/config/ctrl/A/enc/1": (1,)}
    restore_fn = MagicMock(return_value=[])
    restore_assignments_fn = MagicMock(return_value=[])
    watchdog = Watchdog(
        osc=MagicMock(spec=OscConnection),
        diagnostics=diagnostics,
        state=app_state,
        snapshot_provider=lambda: routing_snapshot,
        restore_fn=restore_fn,
        assign_set_snapshot_provider=lambda: assign_set_snapshot,
        restore_assignments_fn=restore_assignments_fn,
    )

    watchdog.trigger_full_restore(reason="test")

    restore_fn.assert_called_once()
    restore_assignments_fn.assert_called_once()
    assert restore_assignments_fn.call_args.args[2] is assign_set_snapshot


def test_trigger_full_restore_restores_assign_sets_even_without_routing_snapshot(diagnostics, app_state):
    assign_set_snapshot = {"/config/ctrl/A/enc/1": (1,)}
    restore_fn = MagicMock(return_value=[])
    restore_assignments_fn = MagicMock(return_value=[])
    watchdog = Watchdog(
        osc=MagicMock(spec=OscConnection),
        diagnostics=diagnostics,
        state=app_state,
        snapshot_provider=lambda: None,
        restore_fn=restore_fn,
        assign_set_snapshot_provider=lambda: assign_set_snapshot,
        restore_assignments_fn=restore_assignments_fn,
    )

    mismatches = watchdog.trigger_full_restore(reason="test")

    restore_fn.assert_not_called()
    restore_assignments_fn.assert_called_once()
    assert mismatches == []

    events = diagnostics.get_recent(5)
    assert not any(e["payload"]["event"] == "restore_skipped_no_snapshot" for e in events if e["category"] == "watchdog")


def test_trigger_full_restore_logs_watchdog_events(diagnostics, app_state):
    snapshot = _make_snapshot()
    watchdog = _make_watchdog(diagnostics, app_state, snapshot=snapshot)
    watchdog.trigger_full_restore(reason="unit-test")

    events = [e for e in diagnostics.get_recent(10) if e["category"] == "watchdog"]
    kinds = [e["payload"]["event"] for e in events]
    assert "crash_restore_triggered" in kinds
    assert "crash_restore_completed" in kinds


def test_start_and_stop_install_and_restore_handlers(diagnostics, app_state):
    watchdog = _make_watchdog(diagnostics, app_state)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_excepthook = sys.excepthook

    watchdog.start()
    try:
        assert signal.getsignal(signal.SIGTERM) == watchdog._handle_signal
        assert sys.excepthook == watchdog._handle_exception
    finally:
        watchdog.stop()

    assert signal.getsignal(signal.SIGTERM) == original_sigterm
    assert sys.excepthook is original_excepthook


def test_start_is_idempotent(diagnostics, app_state):
    watchdog = _make_watchdog(diagnostics, app_state)
    watchdog.start()
    try:
        handler_after_first_start = signal.getsignal(signal.SIGTERM)
        watchdog.start()  # must not stack another atexit registration or lose the original handler
        assert signal.getsignal(signal.SIGTERM) == handler_after_first_start
    finally:
        watchdog.stop()


def test_handle_signal_triggers_restore_and_reraises_default(monkeypatch, diagnostics, app_state):
    snapshot = _make_snapshot()
    restore_fn = MagicMock(return_value=[])
    watchdog = _make_watchdog(diagnostics, app_state, snapshot=snapshot, restore_fn=restore_fn)
    raise_signal = MagicMock()
    monkeypatch.setattr(signal, "raise_signal", raise_signal)

    original_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        watchdog._handle_signal(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, original_sigterm)

    restore_fn.assert_called_once()
    raise_signal.assert_called_once_with(signal.SIGTERM)


def test_handle_atexit_triggers_restore(diagnostics, app_state):
    snapshot = _make_snapshot()
    restore_fn = MagicMock(return_value=[])
    watchdog = _make_watchdog(diagnostics, app_state, snapshot=snapshot, restore_fn=restore_fn)

    watchdog._handle_atexit()
    restore_fn.assert_called_once()


def test_handle_exception_logs_error_restores_and_chains_previous_hook(diagnostics, app_state):
    snapshot = _make_snapshot()
    restore_fn = MagicMock(return_value=[])
    watchdog = _make_watchdog(diagnostics, app_state, snapshot=snapshot, restore_fn=restore_fn)
    previous_hook = MagicMock()
    watchdog._previous_excepthook = previous_hook

    try:
        raise ValueError("boom")
    except ValueError:
        exc_type, exc_value, exc_tb = sys.exc_info()
        watchdog._handle_exception(exc_type, exc_value, exc_tb)

    restore_fn.assert_called_once()
    previous_hook.assert_called_once_with(exc_type, exc_value, exc_tb)

    events = [e for e in diagnostics.get_recent(10) if e["category"] == "error"]
    assert any(e["payload"]["error_type"] == "ValueError" for e in events)


def test_end_to_end_restore_via_real_osc(fake_x32, diagnostics, app_state):
    for addr in addresses.ALL_USERROUT_IN:
        fake_x32.extra_responses[addr] = (5,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    snapshot = _make_snapshot()  # every channel snapshotted at 0
    app_state.channels[1].inserted = True
    app_state.channels[1].card_out_slot = 2

    watchdog = Watchdog(osc=osc, diagnostics=diagnostics, state=app_state, snapshot_provider=lambda: snapshot)
    try:
        mismatches = watchdog.trigger_full_restore(reason="test-crash")
    finally:
        osc.close()

    assert mismatches == []
    assert fake_x32.extra_responses[addresses.userrout_in_addr(1)] == (0,)
    assert app_state.channels[1].inserted is False
