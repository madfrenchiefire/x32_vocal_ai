"""Console discovery: broadcast /xinfo and collect replies.

X32/M32 consoles don't advertise themselves proactively -- there's no push
discovery protocol -- but any console replies to a bare /xinfo query sent
directly to it. Broadcasting that same query to the local subnet's
broadcast address (255.255.255.255 by default) and collecting whichever
replies come back within a short window works because a UDP broadcast
reaches every device on the local L2 segment in one packet, and each
console just answers as it would to a normal unicast query -- this is the
same technique console remote apps use to "find" a console instead of
requiring the user to already know its IP.

Known limitation: sends to one target/broadcast address per call. A PC
with multiple network interfaces on different subnets (e.g. a wired
console NIC and a separate Wi-Fi NIC) needs one call per subnet's
broadcast address -- not auto-enumerated here.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

from app.osc import addresses

DEFAULT_BROADCAST_ADDRESS = "255.255.255.255"
DEFAULT_PORT = 10023
DEFAULT_TIMEOUT_SEC = 2.0


@dataclass
class DiscoveredConsole:
    host: str
    port: int
    name: str
    model: str
    version: str


def discover_consoles(
    target_address: str = DEFAULT_BROADCAST_ADDRESS,
    port: int = DEFAULT_PORT,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> list[DiscoveredConsole]:
    """Sends one /xinfo query to target_address:port and collects every
    distinct-by-source-IP reply within timeout_sec. Returns [] if nothing
    replies (no console on the network, or the target/broadcast address
    doesn't reach one). target_address can be a specific console's IP too
    (a plain unicast query, no SO_BROADCAST semantics needed) -- this is
    what the test suite uses instead of a real broadcast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.2)

    try:
        builder = OscMessageBuilder(address=addresses.XINFO)
        sock.sendto(builder.build().dgram, (target_address, port))

        found: dict[str, DiscoveredConsole] = {}
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                data, (source_ip, _source_port) = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break

            if source_ip in found:
                continue
            try:
                msg = OscMessage(data)
            except Exception:
                continue
            if msg.address != addresses.XINFO or len(msg.params) < 4:
                continue

            ip, name, model, version = (str(p) for p in msg.params[:4])
            found[source_ip] = DiscoveredConsole(host=source_ip, port=port, name=name, model=model, version=version)

        return list(found.values())
    finally:
        sock.close()
