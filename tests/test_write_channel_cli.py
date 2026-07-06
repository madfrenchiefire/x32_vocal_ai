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

    # A console that always reports 0 no matter what was written --
    # simulates a write being silently ignored.
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
