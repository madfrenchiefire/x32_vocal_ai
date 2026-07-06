from __future__ import annotations

from app.osc import addresses
from app.tools import test_write_channel


def test_write_and_readback_matches(fake_x32, tmp_path, capsys):
    channel = 9
    channel_addr = addresses.userrout_in_addr(channel)
    block_addr = addresses.ROUTING_IN_BLOCKS[(channel - 1) // 8]
    fake_x32.extra_responses[channel_addr] = (0,)
    fake_x32.extra_responses[block_addr] = (1,)

    rc = test_write_channel.main([
        "--console", "127.0.0.1",
        "--port", str(fake_x32.port),
        "--channel", str(channel),
        "--value", "5",
        "--timeout", "1.0",
        "--log-dir", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"Before: {channel_addr} = 0" in out
    assert "Match confirmed by readback" in out
    assert f"{channel_addr} = 5" in out
    assert "Local Analog 5" in out
    assert f"{block_addr} = 20" in out
    assert "To revert, rerun with: --channel 9 --value 0 --block-value 1" in out


def test_skip_block_flip_only_touches_channel(fake_x32, tmp_path, capsys):
    channel = 9
    channel_addr = addresses.userrout_in_addr(channel)
    block_addr = addresses.ROUTING_IN_BLOCKS[(channel - 1) // 8]
    fake_x32.extra_responses[channel_addr] = (0,)
    fake_x32.extra_responses[block_addr] = (1,)

    rc = test_write_channel.main([
        "--console", "127.0.0.1",
        "--port", str(fake_x32.port),
        "--channel", str(channel),
        "--value", "5",
        "--skip-block-flip",
        "--timeout", "1.0",
        "--log-dir", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"{block_addr} = 1" in out  # unchanged
    assert fake_x32.extra_responses[block_addr] == (1,)


def test_mismatch_is_reported_when_console_ignores_the_write(monkeypatch, tmp_path, capsys):
    import app.osc.connection as connection_module

    monkeypatch.setattr(test_write_channel, "READBACK_RETRY_DELAY_SEC", 0.01)

    # A console that always reports 0 no matter what was written --
    # simulates a write being silently ignored (never settles, unlike the
    # settle-delay case below).
    monkeypatch.setattr(connection_module.OscConnection, "connect", lambda self, correlation_id=None: None)
    monkeypatch.setattr(connection_module.OscConnection, "close", lambda self: None)
    monkeypatch.setattr(connection_module.OscConnection, "send", lambda self, *a, **k: None)
    monkeypatch.setattr(connection_module.OscConnection, "query", lambda self, *a, **k: (0,))

    rc = test_write_channel.main([
        "--console", "127.0.0.1",
        "--channel", "9",
        "--value", "5",
        "--timeout", "0.3",
        "--log-dir", str(tmp_path),
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "MISMATCH" in captured.err


def test_readback_retries_until_settled(monkeypatch, tmp_path, capsys):
    # Confirmed 2026-07-06 against real hardware: a block-routing write's
    # readback can lag behind the actual (already-applied) change by a
    # couple of query round trips. This reproduces that: the block address
    # reads stale for its first two queries, then settles.
    import app.osc.connection as connection_module

    channel_addr = addresses.userrout_in_addr(9)
    block_addr = addresses.ROUTING_IN_BLOCKS[1]
    call_counts: dict[str, int] = {}

    def fake_query(self, address, args=(), timeout=None, correlation_id=None):
        call_counts[address] = call_counts.get(address, 0) + 1
        if address == channel_addr:
            return (5,)
        if address == block_addr:
            return (1,) if call_counts[address] <= 2 else (20,)
        raise AssertionError(f"unexpected address queried: {address}")

    monkeypatch.setattr(test_write_channel, "READBACK_RETRY_DELAY_SEC", 0.01)
    monkeypatch.setattr(connection_module.OscConnection, "connect", lambda self, correlation_id=None: None)
    monkeypatch.setattr(connection_module.OscConnection, "close", lambda self: None)
    monkeypatch.setattr(connection_module.OscConnection, "send", lambda self, *a, **k: None)
    monkeypatch.setattr(connection_module.OscConnection, "query", fake_query)

    rc = test_write_channel.main([
        "--console", "127.0.0.1",
        "--channel", "9",
        "--value", "5",
        "--timeout", "0.3",
        "--log-dir", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Match confirmed by readback" in out
    assert f"{block_addr} = 20" in out
