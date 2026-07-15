"""Online activation client (app.licensing online mode).

Talks to the license server (cloud/) over plain HTTPS:

  activate(key)  -> POST {app, key, machineCode} to <server>/activate
  refresh()      -> POST {app, key, machineCode} to <server>/check

Both return a short-lived Ed25519-signed token the app verifies locally
(embedded public key) and stores. The token's `recheck` datetime is the hard
offline horizon -- the app keeps working until then and tries to refresh
weekly, well before. Fail-safe: a *network* failure never locks the app (it
keeps its current token); only an explicit server verdict
(disabled/expired/wrong_machine/invalid) clears it.

The HTTP transport is injectable so this is testable without a live server.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.licensing.keys import LicenseError, machine_code, verify_token
from app.licensing.store import LicenseStore

# transport(url, payload) -> response dict. Raises OnlineUnreachable on a
# network-level failure (so the caller can distinguish "can't reach server"
# from "server said no").
Transport = Callable[[str, dict], dict]

REQUEST_TIMEOUT_SEC = 10.0


class OnlineUnreachable(Exception):
    """The license server could not be reached (network/timeout/5xx). NOT an
    authoritative rejection -- the app should stay on its cached token."""


def _default_transport(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if 500 <= exc.code < 600:
            raise OnlineUnreachable(f"server error {exc.code}") from exc
        # 4xx with a JSON body still carries an {"error": ...} verdict.
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise OnlineUnreachable(f"http {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OnlineUnreachable(str(exc)) from exc


class OnlineLicenseClient:
    def __init__(
        self,
        config: AppConfig,
        store: LicenseStore,
        diagnostics: DiagnosticsLogger,
        public_key_hex: str,
        transport: Transport | None = None,
        fingerprint: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.diagnostics = diagnostics
        self.public_key_hex = public_key_hex
        self.transport = transport or _default_transport
        self._machine_code = machine_code(fingerprint)

    def _url(self, endpoint: str) -> str:
        base = (self.config.license_server_url or "").rstrip("/")
        if not base:
            raise OnlineUnreachable("license_server_url is not configured")
        return f"{base}/{endpoint}"

    def activate(self, key: str) -> None:
        """Activate a license key on this machine. Stores the returned token.
        Raises LicenseError on an authoritative rejection, OnlineUnreachable
        on a network failure."""
        key = key.strip()
        resp = self.transport(self._url("activate"), {
            "app": self.config.product_id, "key": key, "machineCode": self._machine_code,
        })
        self._store_token_or_raise(resp)
        self.diagnostics.log_user_action("license_online_activated", {"key_prefix": key[:8]})

    def refresh(self) -> bool:
        """Re-check the stored license with the server and refresh its token.
        Returns True if refreshed, False if there's nothing to refresh.
        Raises LicenseError on an authoritative rejection (caller should then
        lock), OnlineUnreachable on a network failure (caller keeps cached)."""
        token = self.store.load_token()
        if not token:
            return False
        try:
            info = verify_token(token, self.public_key_hex)
        except LicenseError:
            return False
        resp = self.transport(self._url("check"), {
            "app": self.config.product_id, "key": info.id, "machineCode": self._machine_code,
        })
        self._store_token_or_raise(resp)
        return True

    def is_refresh_due(self, within_days: float = 4.0, now: datetime | None = None) -> bool:
        """True if the stored token's recheck horizon is within `within_days`
        (or already past) -- i.e. time to phone home. False if there's no
        online token to refresh."""
        token = self.store.load_token()
        if not token:
            return False
        try:
            info = verify_token(token, self.public_key_hex)
        except LicenseError:
            return False
        if not info.recheck:
            return False
        try:
            horizon = datetime.fromisoformat(info.recheck)
        except ValueError:
            return False
        if horizon.tzinfo is None:
            horizon = horizon.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return now >= horizon - timedelta(days=within_days)

    def _store_token_or_raise(self, resp: dict) -> None:
        if resp.get("error"):
            # Authoritative rejection: clear the cached token so status() drops
            # to trial/unlicensed rather than trusting a now-revoked license.
            self.store.clear_token()
            raise LicenseError(_message_for(resp["error"], self._machine_code))
        token = resp.get("token")
        if not token:
            raise OnlineUnreachable("server returned no token")
        # Verify before trusting/storing -- a wrong-product or bad-signature
        # token is rejected here.
        info = verify_token(token, self.public_key_hex)
        if not info.matches_product(self.config.product_id):
            raise LicenseError("server returned a token for a different product")
        self.store.save_token(token)


def _message_for(error: str, code: str) -> str:
    return {
        "invalid": "License key not recognized.",
        "disabled": "This license has been disabled. Contact support.",
        "expired": "This license has expired.",
        "wrong_machine": f"This license is active on another computer. Release it there first, "
                         f"or contact support with machine code {code}.",
    }.get(error, f"License error: {error}")


class LicenseRefresher:
    """Background thread (online mode) that phones home when the stored
    token nears its recheck horizon -- so the token keeps rolling forward
    while the app is online, and a disabled/expired license is caught within
    ~a token lifetime. A network failure is swallowed (stay on the cached
    token); an authoritative rejection clears the token (the license gate
    then locks the app)."""

    def __init__(
        self,
        client: OnlineLicenseClient,
        diagnostics: DiagnosticsLogger,
        tick_sec: float = 6 * 3600,
        within_days: float = 4.0,
    ) -> None:
        self.client = client
        self.diagnostics = diagnostics
        self.tick_sec = tick_sec
        self.within_days = within_days
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="license-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def refresh_now(self) -> None:
        """One refresh attempt if due -- run on startup and each tick."""
        try:
            if self.client.is_refresh_due(self.within_days):
                self.client.refresh()
        except OnlineUnreachable:
            pass  # offline -- keep the cached token, try again next tick
        except LicenseError as exc:
            self.diagnostics.log_watchdog("license_refresh_rejected", {"reason": str(exc)})

    def _loop(self) -> None:
        self.refresh_now()
        while not self._stop.wait(self.tick_sec):
            self.refresh_now()
