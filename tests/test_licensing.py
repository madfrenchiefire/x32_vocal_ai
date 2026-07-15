from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.licensing import keys
from app.licensing.manager import LicenseManager
from app.licensing.store import LicenseStore


@pytest.fixture
def keypair():
    return keys.generate_keypair()  # (private_hex, public_hex)


@pytest.fixture
def store(tmp_path):
    return LicenseStore(tmp_path / "licdata")


def _now(offset_days=0):
    return datetime.now(timezone.utc) + timedelta(days=offset_days)


# -- keys -------------------------------------------------------------------


def test_sign_verify_roundtrip(keypair):
    priv, pub = keypair
    token = keys.sign_token(keys.build_payload(name="Jane", email="j@x.com", tier="pro"), priv)
    info = keys.verify_token(token, pub)
    assert info.name == "Jane"
    assert info.email == "j@x.com"
    assert info.tier == "pro"
    assert info.machine is None


def test_tampered_token_rejected(keypair):
    priv, pub = keypair
    token = keys.sign_token(keys.build_payload(name="Jane"), priv)
    tampered = token[:-6] + ("abcdef" if token[-6:] != "abcdef" else "abcde0")
    with pytest.raises(keys.LicenseError):
        keys.verify_token(tampered, pub)


def test_wrong_public_key_rejected(keypair):
    priv, _pub = keypair
    _priv2, pub2 = keys.generate_keypair()
    token = keys.sign_token(keys.build_payload(name="Jane"), priv)
    with pytest.raises(keys.LicenseError):
        keys.verify_token(token, pub2)


def test_malformed_token_rejected(keypair):
    _priv, pub = keypair
    with pytest.raises(keys.LicenseError):
        keys.verify_token("not-a-token", pub)


def test_expiry(keypair):
    priv, pub = keypair
    past = (_now(-1)).date().isoformat()
    future = (_now(365)).date().isoformat()
    assert keys.verify_token(keys.sign_token(keys.build_payload(name="x", expires=past), priv), pub).is_expired()
    assert not keys.verify_token(keys.sign_token(keys.build_payload(name="x", expires=future), priv), pub).is_expired()


def test_machine_lock_and_normalization(keypair):
    priv, pub = keypair
    token = keys.sign_token(keys.build_payload(name="x", machine="3F9A-2C1B-7E04-9D8E-1A2B"), priv)
    info = keys.verify_token(token, pub)
    assert info.matches_machine("3f9a2c1b7e049d8e1a2b")  # case/dash-insensitive
    assert info.matches_machine("3F9A-2C1B-7E04-9D8E-1A2B")
    assert not info.matches_machine("AAAA-BBBB-CCCC-DDDD-EEEE")


def test_floating_key_matches_any_machine(keypair):
    priv, pub = keypair
    info = keys.verify_token(keys.sign_token(keys.build_payload(name="x"), priv), pub)
    assert info.matches_machine("anything")


def test_machine_code_format():
    code = keys.machine_code("abcdef0123456789abcdef0123456789")
    assert code == "ABCD-EF01-2345-6789-ABCD"


# -- manager ----------------------------------------------------------------


def _manager(store, pub, **kw):
    kw.setdefault("fingerprint", "testfingerprint")
    return LicenseManager(store=store, public_key_hex=pub, **kw)


def test_trial_starts_and_counts_down(store, keypair):
    _priv, pub = keypair
    now = [_now()]
    mgr = _manager(store, pub, trial_days=14, now_provider=lambda: now[0])
    assert mgr.status().state == "trial"
    assert mgr.status().trial_days_remaining == 14
    now[0] = _now(10)
    assert mgr.status().trial_days_remaining == 4


def test_trial_expires(store, keypair):
    _priv, pub = keypair
    now = [_now()]
    mgr = _manager(store, pub, trial_days=14, now_provider=lambda: now[0])
    mgr.status()  # start the trial clock
    now[0] = _now(20)
    st = mgr.status()
    assert st.state == "trial_expired"
    assert not st.functional


def test_trial_clock_rollback_does_not_extend(store, keypair):
    _priv, pub = keypair
    now = [_now()]
    mgr = _manager(store, pub, trial_days=14, now_provider=lambda: now[0])
    mgr.status()  # start the trial clock at day 0
    now[0] = _now(10)
    mgr.status()  # last_seen advances to +10 (4 days remaining)
    now[0] = _now(0)  # clock rolled back
    # Rollback shouldn't hand back the elapsed days: still 4, not reset to 14.
    assert mgr.status().trial_days_remaining == 4


def test_activate_valid_key_becomes_licensed(store, keypair):
    priv, pub = keypair
    mgr = _manager(store, pub)
    token = keys.sign_token(keys.build_payload(name="Buyer", tier="pro"), priv)
    st = mgr.activate(token)
    assert st.state == "licensed"
    assert st.functional
    assert st.licensee == "Buyer"
    # Persisted: a fresh manager on the same store is licensed too.
    assert _manager(store, pub).status().state == "licensed"


def test_activate_expired_key_raises(store, keypair):
    priv, pub = keypair
    mgr = _manager(store, pub)
    token = keys.sign_token(keys.build_payload(name="x", expires=_now(-1).date().isoformat()), priv)
    with pytest.raises(keys.LicenseError, match="expired"):
        mgr.activate(token)


def test_activate_wrong_machine_raises(store, keypair):
    priv, pub = keypair
    mgr = _manager(store, pub, fingerprint="thispc")
    token = keys.sign_token(keys.build_payload(name="x", machine="OTHER-PC-CODE"), priv)
    with pytest.raises(keys.LicenseError, match="different computer"):
        mgr.activate(token)


def test_deactivate_reverts_to_trial(store, keypair):
    priv, pub = keypair
    mgr = _manager(store, pub, trial_days=14)
    mgr.activate(keys.sign_token(keys.build_payload(name="x"), priv))
    assert mgr.status().state == "licensed"
    mgr.deactivate()
    assert mgr.status().state == "trial"


def test_junk_stored_token_falls_through_to_trial(store, keypair):
    _priv, pub = keypair
    store.save_token("X32SNIPER1.garbage.garbage")
    mgr = _manager(store, pub, trial_days=14)
    assert mgr.status().state == "trial"  # junk token != a license, but trial still valid


# -- store ------------------------------------------------------------------


def test_store_token_roundtrip(tmp_path):
    store = LicenseStore(tmp_path / "d")
    assert store.load_token() is None
    store.save_token("  X32SNIPER1.a.b  ")
    assert store.load_token() == "X32SNIPER1.a.b"
    store.clear_token()
    assert store.load_token() is None


def test_store_trial_roundtrip(tmp_path):
    store = LicenseStore(tmp_path / "d")
    assert store.load_trial() is None
    rec = store.start_trial("fp")
    assert rec["machine"] == "fp"
    assert store.load_trial()["machine"] == "fp"
