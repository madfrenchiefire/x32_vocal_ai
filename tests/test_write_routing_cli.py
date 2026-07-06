from __future__ import annotations

from app.tools import test_write_routing


def test_write_and_readback_matches_known_address(fake_x32, tmp_path, capsys):
    address = "/config/routing/CARD/9-16"  # known table: rtaea
    fake_x32.extra_responses[address] = (1,)

    rc = test_write_routing.main([
        "--console", "127.0.0.1",
        "--port", str(fake_x32.port),
        "--address", address,
        "--value", "26",
        "--timeout", "1.0",
        "--log-dir", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"Before: {address} = 1 (AN9-16)" in out
    assert f"After:  {address} = 26 (USER)" in out
    assert "Match confirmed by readback" in out
    assert f"To revert, rerun with: --address {address} --value 1" in out


def test_unknown_address_reports_raw_only(fake_x32, tmp_path, capsys):
    address = "/config/routing/SOMETHING/NEW"
    fake_x32.extra_responses[address] = (0,)

    rc = test_write_routing.main([
        "--console", "127.0.0.1",
        "--port", str(fake_x32.port),
        "--address", address,
        "--value", "1",
        "--timeout", "1.0",
        "--log-dir", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "no known decode table" in out
    assert "(no decode table known for this address)" in out


def test_mismatch_is_reported(monkeypatch, tmp_path, capsys):
    import app.osc.connection as connection_module

    monkeypatch.setattr(connection_module.OscConnection, "connect", lambda self, correlation_id=None: None)
    monkeypatch.setattr(connection_module.OscConnection, "close", lambda self: None)
    monkeypatch.setattr(connection_module.OscConnection, "send", lambda self, *a, **k: None)
    monkeypatch.setattr(connection_module.OscConnection, "query", lambda self, *a, **k: (0,))
    monkeypatch.setattr(connection_module.OscConnection, "query_until_match", lambda self, *a, **k: 0)

    rc = test_write_routing.main([
        "--console", "127.0.0.1",
        "--address", "/config/routing/CARD/9-16",
        "--value", "26",
        "--timeout", "0.3",
        "--log-dir", str(tmp_path),
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "MISMATCH" in captured.err
