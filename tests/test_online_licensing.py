"""Tests for online-mode licensing (app.licensing.online) + the app<->server
token round trip. The 'server' is simulated by driving the real cloud
license_core logic through the client's injected transport, so both halves
are exercised together without a live Firebase."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import AppConfig
from app.licensing import keys as app_keys
from app.licensing.manager import LicenseManager
from app.licensing.online import LicenseRefresher, OnlineLicenseClient, OnlineUnreachable
from app.licensing.store import LicenseStore

_spec = importlib.util.spec_from_file_location(
    "license_core", Path(__file__).resolve().parent.parent / "cloud" / "functions" / "license_core.py"
)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

PRODUCT = "x32-sonicsniper"


class FakeServer:
    """Stands in for the deployed activate/check Cloud Functions, using the
    real license_core decision + signing logic against an in-memory DB."""

    def __init__(self, private_hex, licenses, now=None, ttl_days=10):
        self.private_hex = private_hex
        self.db = {l["key"]: dict(l) for l in licenses}
        self.now = now or datetime.now(timezone.utc)
        self.ttl_days = ttl_days
        self.unreachable = False

    def transport(self, url, payload):
        if self.unreachable:
            raise OnlineUnreachable("simulated network down")
        endpoint = url.rsplit("/", 1)[-1]
        key, machine, app_id = payload["key"], payload["machineCode"], payload["app"]
        lic = self.db.get(key)
        if not core.product_matches(lic, app_id):
            return {"error": "invalid"}
        if endpoint == "release":
            result, updates = core.decide_release(lic, machine, self.now)
            if result != "ok":
                return {"error": result}
            if updates:
                self.db[key].update(updates)
            return {"ok": True}
        decide = core.decide_activation if endpoint == "activate" else core.decide_check
        result, updates = decide(lic, machine, self.now)
        if result != "ok":
            return {"error": result}
        self.db[key].update(updates)
        payload_out = core.build_online_payload(self.db[key], machine, self.now, self.ttl_days)
        return {"token": core.sign_online_token(payload_out, self.private_hex)}


def _license(key="XVAI-AAAA-BBBB-CCCC", **over):
    base = {"key": key, "ownerEmail": "j@x.com", "ownerName": "J", "productId": PRODUCT,
            "type": "lifetime", "tier": "pro", "status": "active", "machineCode": None,
            "rebindCount": 0, "expires": None}
    base.update(over)
    return base


def _client(tmp_path, server, pub, **cfg):
    config = AppConfig(license_mode="online", license_server_url="https://x/", product_id=PRODUCT, **cfg)
    store = LicenseStore(tmp_path / "lic")
    from app.diagnostics.logger import DiagnosticsLogger
    diag = DiagnosticsLogger(log_dir=tmp_path / "logs", ring_buffer_size=100, state_provider=lambda: {})
    client = OnlineLicenseClient(config, store, diag, pub, transport=server.transport, fingerprint="pc-1")
    return client, store, config


def test_activate_binds_and_stores_token(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client, store, config = _client(tmp_path, server, pub)

    client.activate("XVAI-AAAA-BBBB-CCCC")

    token = store.load_token()
    assert token is not None
    info = app_keys.verify_token(token, pub)
    assert info.app == PRODUCT
    assert info.recheck is not None
    # server bound this machine
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["machineCode"] == client._machine_code


def test_activate_wrong_product_key_rejected(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license(productId="some-other-app")])
    client, store, _ = _client(tmp_path, server, pub)
    with pytest.raises(app_keys.LicenseError):
        client.activate("XVAI-AAAA-BBBB-CCCC")
    assert store.load_token() is None


def test_disabled_key_rejected_and_clears_token(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license(status="disabled")])
    client, store, _ = _client(tmp_path, server, pub)
    with pytest.raises(app_keys.LicenseError, match="disabled"):
        client.activate("XVAI-AAAA-BBBB-CCCC")
    assert store.load_token() is None


def test_activate_on_second_machine_fails(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license(machineCode="OTHER-PC")])
    client, store, _ = _client(tmp_path, server, pub)
    with pytest.raises(app_keys.LicenseError, match="another computer"):
        client.activate("XVAI-AAAA-BBBB-CCCC")


def test_network_failure_is_not_authoritative(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client, store, _ = _client(tmp_path, server, pub)
    server.unreachable = True
    with pytest.raises(OnlineUnreachable):
        client.activate("XVAI-AAAA-BBBB-CCCC")


def test_manager_reports_licensed_after_online_activation(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client, store, config = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")

    mgr = LicenseManager(store=store, public_key_hex=pub, product_id=PRODUCT, fingerprint="pc-1")
    assert mgr.status().state == "licensed"


def test_manager_rejects_token_for_other_product(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client, store, _ = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")
    # A manager for a DIFFERENT product must not accept this token.
    mgr = LicenseManager(store=store, public_key_hex=pub, product_id="another-app", fingerprint="pc-1")
    assert mgr.status().state in ("trial", "trial_expired")


def test_recheck_required_past_horizon(tmp_path):
    priv, pub = app_keys.generate_keypair()
    # Issue a token whose recheck horizon is already 1 day in the past.
    past = datetime.now(timezone.utc) - timedelta(days=11)
    server = FakeServer(priv, [_license()], now=past, ttl_days=10)
    client, store, _ = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")

    mgr = LicenseManager(store=store, public_key_hex=pub, product_id=PRODUCT, fingerprint="pc-1")
    st = mgr.status()
    assert st.state == "recheck_required"
    assert not st.functional


def test_refresh_extends_recheck(tmp_path):
    priv, pub = app_keys.generate_keypair()
    # First token issued 8 days ago (recheck ~2 days out -> refresh due).
    old = datetime.now(timezone.utc) - timedelta(days=8)
    server = FakeServer(priv, [_license()], now=old, ttl_days=10)
    client, store, _ = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")
    assert client.is_refresh_due(within_days=4)

    # Server clock is now "today" -> a refresh should push recheck ~10 days out.
    server.now = datetime.now(timezone.utc)
    assert client.refresh() is True
    assert not client.is_refresh_due(within_days=4)


def test_refresher_locks_on_authoritative_rejection(tmp_path):
    priv, pub = app_keys.generate_keypair()
    old = datetime.now(timezone.utc) - timedelta(days=8)
    server = FakeServer(priv, [_license()], now=old, ttl_days=10)
    client, store, _ = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")

    # Admin disables the license; the next refresh clears the cached token.
    server.db["XVAI-AAAA-BBBB-CCCC"]["status"] = "disabled"
    server.now = datetime.now(timezone.utc)
    from app.diagnostics.logger import DiagnosticsLogger
    diag = DiagnosticsLogger(log_dir=tmp_path / "logs2", ring_buffer_size=100, state_provider=lambda: {})
    refresher = LicenseRefresher(client, diag)
    refresher.refresh_now()
    assert store.load_token() is None


def test_deactivate_releases_binding_and_clears_token(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client, store, _ = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["machineCode"] == client._machine_code
    assert store.load_token() is not None

    client.deactivate()

    # Server binding released (machine cleared, rebind counted) and the local
    # token dropped -- the license can now be activated elsewhere.
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["machineCode"] is None
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["rebindCount"] == 1
    assert store.load_token() is None


def test_deactivated_license_reactivates_on_another_machine(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client_a, store_a, _ = _client(tmp_path / "a", server, pub)
    client_a.activate("XVAI-AAAA-BBBB-CCCC")
    client_a.deactivate()

    # A different machine can now claim it (would have been wrong_machine before).
    config_b = AppConfig(license_mode="online", license_server_url="https://x/", product_id=PRODUCT)
    store_b = LicenseStore(tmp_path / "b" / "lic")
    from app.diagnostics.logger import DiagnosticsLogger
    diag = DiagnosticsLogger(log_dir=tmp_path / "b" / "logs", ring_buffer_size=100, state_provider=lambda: {})
    client_b = OnlineLicenseClient(config_b, store_b, diag, pub, transport=server.transport, fingerprint="pc-2")
    client_b.activate("XVAI-AAAA-BBBB-CCCC")
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["machineCode"] == client_b._machine_code
    assert store_b.load_token() is not None


def test_deactivate_network_failure_keeps_local_token(tmp_path):
    priv, pub = app_keys.generate_keypair()
    server = FakeServer(priv, [_license()])
    client, store, _ = _client(tmp_path, server, pub)
    client.activate("XVAI-AAAA-BBBB-CCCC")

    server.unreachable = True
    with pytest.raises(OnlineUnreachable):
        client.deactivate()
    # Couldn't reach the server to release -- must NOT clear locally (else the
    # binding is stranded and the license can't be moved).
    assert store.load_token() is not None
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["machineCode"] == client._machine_code


def test_deactivate_wrong_machine_raises_and_keeps_token(tmp_path):
    priv, pub = app_keys.generate_keypair()
    # License bound to a DIFFERENT machine than this client (pc-1).
    server = FakeServer(priv, [_license(machineCode="pc-other")])
    client, store, _ = _client(tmp_path, server, pub)
    # Hand-place a token so deactivate has a key to release.
    payload = core.build_online_payload(server.db["XVAI-AAAA-BBBB-CCCC"], "pc-1", server.now)
    store.save_token(core.sign_online_token(payload, priv))

    from app.licensing.keys import LicenseError
    with pytest.raises(LicenseError):
        client.deactivate()
    # A machine that doesn't hold the lock can't release it, and its token stays.
    assert store.load_token() is not None
    assert server.db["XVAI-AAAA-BBBB-CCCC"]["machineCode"] == "pc-other"
