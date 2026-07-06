"""OSC connection manager for the X32 console.

Owns the single UDP socket used to talk to the console: connects, checks
firmware, keeps the console's OSC subscription alive with periodic
``/xremote``, and detects + recovers from silent disconnects. Every byte
sent or received goes through :class:`app.diagnostics.logger.DiagnosticsLogger`
first -- this is the one place in the app that is allowed to touch a raw
OSC socket.

Nothing here writes console *parameters* (routing apply/MIDI assignment
etc. are separate, not-yet-implemented modules) -- this module only
connects, subscribes, and answers request/reply queries used by read-only
callers such as the routing snapshot reader.
"""
from __future__ import annotations

import queue
import re
import socket
import threading
import time
from typing import Any

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.state import AppState

RECV_BUFFER_SIZE = 8192
RECV_POLL_TIMEOUT_SEC = 0.5
WATCHDOG_TICK_SEC = 1.0


class OscConnectionError(Exception):
    pass


class FirmwareTooOldError(OscConnectionError):
    pass


def _parse_version(version_str: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\.(\d+)", version_str)
    if not match:
        raise ValueError(f"could not parse a version number out of {version_str!r}")
    return int(match.group(1)), int(match.group(2))


def _build_message(address: str, args: tuple) -> bytes:
    builder = OscMessageBuilder(address=address)
    for arg in args:
        builder.add_arg(arg)
    return builder.build().dgram


class OscConnection:
    def __init__(
        self,
        host: str,
        port: int,
        diagnostics: DiagnosticsLogger,
        state: AppState | None = None,
        local_port: int = 0,
        timeout_sec: float = 2.0,
        xremote_interval_sec: float = 8.0,
        min_firmware: str = "4.0",
        reconnect_backoff_sec: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
    ) -> None:
        self.host = host
        self.port = port
        self.local_port = local_port
        self.timeout_sec = timeout_sec
        self.xremote_interval_sec = xremote_interval_sec
        self.min_firmware = min_firmware
        self.reconnect_backoff_sec = reconnect_backoff_sec

        self._diagnostics = diagnostics
        self._state = state

        self._sock: socket.socket | None = None
        # Cleared while running; set() requests all background threads to
        # stop. threading.Event.wait(timeout) blocks for the full timeout
        # and returns False as long as the event stays clear, which is what
        # gives the keepalive/watchdog loops an interruptible sleep -- do
        # not flip this to "set while running", since Event.wait() returns
        # immediately (not after the timeout) once the event is already set.
        self._stop_event = threading.Event()
        self._recv_thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None

        self._pending_lock = threading.Lock()
        self._pending: dict[str, list[queue.Queue]] = {}

        self._state_lock = threading.Lock()
        self._connected = False
        self._last_rx_monotonic: float | None = None
        self.xinfo: dict[str, str] = {}

    # -- public state -----------------------------------------------------
    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    # -- lifecycle ----------------------------------------------------------
    def connect(self, correlation_id: str | None = None) -> None:
        """Open the socket, verify firmware via /xinfo, and start the
        keepalive + watchdog threads. Raises FirmwareTooOldError if the
        console reports firmware below min_firmware, or OscConnectionError
        for any other failure to establish contact."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", self.local_port))
        self._sock.settimeout(RECV_POLL_TIMEOUT_SEC)
        self.local_port = self._sock.getsockname()[1]

        self._stop_event.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, name="osc-recv", daemon=True)
        self._recv_thread.start()

        try:
            self._handshake(correlation_id)
        except Exception:
            self._stop_event.set()
            self._sock.close()
            raise

        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="osc-keepalive", daemon=True
        )
        self._keepalive_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="osc-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _handshake(self, correlation_id: str | None) -> None:
        try:
            args = self.query(addresses.XINFO, timeout=self.timeout_sec, correlation_id=correlation_id)
        except TimeoutError as exc:
            raise OscConnectionError(
                f"no reply to {addresses.XINFO} from {self.host}:{self.port} within {self.timeout_sec}s"
            ) from exc

        keys = ("ip", "name", "model", "version")
        self.xinfo = dict(zip(keys, [str(a) for a in args]))

        version_str = self.xinfo.get("version", "")
        try:
            firmware = _parse_version(version_str)
            required = _parse_version(self.min_firmware)
        except ValueError as exc:
            raise OscConnectionError(f"unparseable firmware version in /xinfo reply: {self.xinfo}") from exc

        if firmware < required:
            raise FirmwareTooOldError(
                f"console firmware {version_str} is below the required {self.min_firmware} "
                "(User In/Out routing needs 4.0+)"
            )

        with self._state_lock:
            self._connected = True
            self._last_rx_monotonic = time.monotonic()

        if self._state is not None:
            self._state.update_connection(
                connected=True,
                host=self.host,
                port=self.port,
                console_name=self.xinfo.get("name"),
                model=self.xinfo.get("model"),
                firmware_version=version_str,
                last_error=None,
                last_seen_monotonic=time.monotonic(),
            )
        self._diagnostics.log_state_change(
            "osc_connected", after=self.xinfo, correlation_id=correlation_id
        )

    def close(self) -> None:
        self._stop_event.set()
        for t in (self._recv_thread, self._keepalive_thread, self._watchdog_thread):
            if t is not None and t.is_alive():
                t.join(timeout=RECV_POLL_TIMEOUT_SEC + 1)
        if self._sock is not None:
            self._sock.close()
        with self._state_lock:
            self._connected = False

    def __enter__(self) -> "OscConnection":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- send / query ------------------------------------------------------
    def send(self, address: str, *args: Any, correlation_id: str | None = None) -> None:
        assert self._sock is not None, "connect() must be called first"
        self._sock.sendto(_build_message(address, args), (self.host, self.port))
        self._diagnostics.log_osc_tx(address, args, correlation_id=correlation_id)

    def query(
        self,
        address: str,
        args: tuple = (),
        timeout: float | None = None,
        correlation_id: str | None = None,
    ) -> tuple:
        """Send address as a "get" (no-arg) message and block for the
        matching reply. Raises TimeoutError if nothing comes back in time."""
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending.setdefault(address, []).append(q)
        try:
            self.send(address, *args, correlation_id=correlation_id)
            try:
                return q.get(timeout=timeout if timeout is not None else self.timeout_sec)
            except queue.Empty:
                raise TimeoutError(f"no reply to {address} within {timeout or self.timeout_sec}s")
        finally:
            with self._pending_lock:
                waiters = self._pending.get(address, [])
                if q in waiters:
                    waiters.remove(q)

    def query_until_match(
        self,
        address: str,
        expected_value: Any,
        attempts: int = 5,
        delay_sec: float = 0.3,
        correlation_id: str | None = None,
    ) -> Any:
        """Query address repeatedly, pausing delay_sec between tries, until
        it reads back as expected_value or attempts run out. Some writes
        (confirmed: block-routing changes) take a moment to settle on the
        console before a subsequent read reflects them -- an immediate
        single query would falsely report those as a failed write. Returns
        the last value read, whether or not it matched."""
        value = expected_value
        for attempt in range(attempts):
            (value,) = self.query(address, correlation_id=correlation_id)
            if value == expected_value:
                if attempt > 0:
                    self._diagnostics.log_watchdog(
                        "readback_settled_after_retry",
                        {"address": address, "attempts": attempt + 1},
                        correlation_id=correlation_id,
                    )
                return value
            time.sleep(delay_sec)
        return value

    def query_many(
        self,
        addresses_: list[str],
        timeout: float | None = None,
        pace_sec: float = 0.005,
        retries: int = 1,
        correlation_id: str | None = None,
    ) -> dict[str, tuple | None]:
        """Query many addresses, paced a few ms apart so as not to flood the
        console (per CLAUDE.md's routing-automation write pacing note --
        applied here to reads too). Missing addresses are retried up to
        `retries` times, then reported as None (not fatal)."""
        deadline_timeout = timeout if timeout is not None else self.timeout_sec
        results: dict[str, tuple | None] = {addr: None for addr in addresses_}
        pending_addrs = list(addresses_)

        for attempt in range(retries + 1):
            if not pending_addrs:
                break
            waiters: dict[str, queue.Queue] = {}
            with self._pending_lock:
                for addr in pending_addrs:
                    q: queue.Queue = queue.Queue(maxsize=1)
                    waiters[addr] = q
                    self._pending.setdefault(addr, []).append(q)

            for addr in pending_addrs:
                self.send(addr, correlation_id=correlation_id)
                time.sleep(pace_sec)

            deadline = time.monotonic() + deadline_timeout
            for addr in list(pending_addrs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    results[addr] = waiters[addr].get(timeout=remaining)
                except queue.Empty:
                    continue

            with self._pending_lock:
                for addr, q in waiters.items():
                    lst = self._pending.get(addr, [])
                    if q in lst:
                        lst.remove(q)

            pending_addrs = [addr for addr in pending_addrs if results[addr] is None]

        return results

    # -- background threads -------------------------------------------------
    def _recv_loop(self) -> None:
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                data, _ = self._sock.recvfrom(RECV_BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    self._diagnostics.log_error(OscConnectionError("recv socket error"))
                continue

            with self._state_lock:
                self._last_rx_monotonic = time.monotonic()

            try:
                msg = OscMessage(data)
            except Exception as exc:
                self._diagnostics.log_error(exc, context="failed to parse incoming OSC packet")
                continue

            self._diagnostics.log_osc_rx(msg.address, tuple(msg.params))

            with self._pending_lock:
                waiters = list(self._pending.get(msg.address, []))
            for q in waiters:
                if not q.full():
                    q.put_nowait(tuple(msg.params))

    def _keepalive_loop(self) -> None:
        while not self._stop_event.wait(self.xremote_interval_sec):
            try:
                self.send(addresses.XREMOTE)
            except OSError as exc:
                self._diagnostics.log_error(exc, context="failed to send /xremote keepalive")

    def _watchdog_loop(self) -> None:
        stale_threshold = self.xremote_interval_sec * 2.5
        backoff_index = 0
        next_probe = 0.0

        while not self._stop_event.wait(WATCHDOG_TICK_SEC):
            now = time.monotonic()
            with self._state_lock:
                last_rx = self._last_rx_monotonic
                was_connected = self._connected

            stale = last_rx is None or (now - last_rx) > stale_threshold

            if was_connected and stale:
                with self._state_lock:
                    self._connected = False
                if self._state is not None:
                    self._state.update_connection(connected=False, last_error="no traffic from console")
                self._diagnostics.log_watchdog(
                    "connection_lost", {"seconds_since_last_rx": None if last_rx is None else now - last_rx}
                )
                backoff_index = 0
                next_probe = now

            if not self.connected and now >= next_probe:
                correlation_id = self._diagnostics.new_correlation_id()
                try:
                    self._handshake(correlation_id)
                    self._diagnostics.log_watchdog("reconnected", {"xinfo": self.xinfo}, correlation_id=correlation_id)
                    backoff_index = 0
                except OscConnectionError as exc:
                    delay = self.reconnect_backoff_sec[min(backoff_index, len(self.reconnect_backoff_sec) - 1)]
                    backoff_index += 1
                    next_probe = now + delay
                    self._diagnostics.log_watchdog(
                        "reconnect_failed", {"error": str(exc), "next_attempt_in_sec": delay},
                        correlation_id=correlation_id,
                    )
