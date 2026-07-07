from __future__ import annotations

import socket

from app.osc.discovery import discover_consoles


def test_discover_consoles_finds_a_replying_console(fake_x32):
    results = discover_consoles(target_address="127.0.0.1", port=fake_x32.port, timeout_sec=0.5)

    assert len(results) == 1
    found = results[0]
    assert found.host == "127.0.0.1"
    assert found.port == fake_x32.port
    assert found.name == "TESTX32"
    assert found.model == "X32"
    assert found.version == fake_x32.version


def test_discover_consoles_returns_empty_when_nothing_replies():
    # Bind an ephemeral socket just to reserve a port nothing is listening
    # on for OSC, then immediately release it.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    unused_port = probe.getsockname()[1]
    probe.close()

    results = discover_consoles(target_address="127.0.0.1", port=unused_port, timeout_sec=0.3)
    assert results == []
