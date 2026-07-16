"""Tests for the Cloud Functions license logic (cloud/functions/license_core.py).

Imported by path so the cloud/ tree stays a standalone Firebase deploy unit
(no package __init__ that would confuse deployment).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.licensing import keys as app_keys

_spec = importlib.util.spec_from_file_location(
    "license_core", Path(__file__).resolve().parent.parent / "cloud" / "functions" / "license_core.py"
)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


def _license(**overrides):
    base = {
        "key": "XVAI-AAAA-BBBB-CCCC",
        "ownerEmail": "jane@example.com",
        "ownerName": "Jane",
        "type": "lifetime",
        "tier": "pro",
        "status": "active",
        "machineCode": None,
        "rebindCount": 0,
        "expires": None,
    }
    base.update(overrides)
    return base


# -- activation -------------------------------------------------------------


def test_activation_binds_unbound_machine():
    result, updates = core.decide_activation(_license(), "3F9A-2C1B-7E04", NOW)
    assert result == "ok"
    assert updates["machineCode"] == "3F9A-2C1B-7E04"
    assert "machineBoundAt" in updates


def test_activation_ok_when_same_machine():
    result, updates = core.decide_activation(_license(machineCode="3f9a2c1b7e04"), "3F9A-2C1B-7E04", NOW)
    assert result == "ok"
    assert "machineCode" not in updates  # already bound, not rebound


def test_activation_rejects_other_machine():
    result, _ = core.decide_activation(_license(machineCode="AAAA"), "BBBB", NOW)
    assert result == "wrong_machine"


def test_activation_invalid_missing_license():
    assert core.decide_activation(None, "m", NOW)[0] == "invalid"


def test_activation_disabled_license():
    assert core.decide_activation(_license(status="disabled"), "m", NOW)[0] == "disabled"


def test_activation_expired_monthly():
    past = (NOW - timedelta(days=1)).isoformat()
    assert core.decide_activation(_license(type="monthly", expires=past), "m", NOW)[0] == "expired"


def test_lifetime_never_expires():
    assert core.decide_activation(_license(type="lifetime", expires=None), "m", NOW)[0] == "ok"


# -- check ------------------------------------------------------------------


def test_check_ok_when_bound_matches():
    result, updates = core.decide_check(_license(machineCode="MMMM"), "mmmm", NOW)
    assert result == "ok"
    assert "lastCheckAt" in updates


def test_check_never_binds_unbound():
    # A check on an unbound license must fail (only activate binds).
    assert core.decide_check(_license(machineCode=None), "m", NOW)[0] == "wrong_machine"


def test_check_disabled_returns_disabled():
    assert core.decide_check(_license(status="disabled", machineCode="m"), "m", NOW)[0] == "disabled"


# -- deactivation -----------------------------------------------------------


def test_owner_can_deactivate():
    result, updates = core.decide_deactivation(_license(machineCode="m", rebindCount=1), "JANE@example.com", False)
    assert result == "ok"
    assert updates["machineCode"] is None
    assert updates["rebindCount"] == 2


def test_non_owner_forbidden():
    assert core.decide_deactivation(_license(), "someone@else.com", False)[0] == "forbidden"


def test_admin_can_deactivate_any():
    assert core.decide_deactivation(_license(), "admin@x.com", True)[0] == "ok"


# -- app-side self-release (decide_release) ---------------------------------


def test_release_by_bound_machine_clears_binding():
    now = datetime.now(timezone.utc)
    result, updates = core.decide_release(_license(machineCode="pc-1", rebindCount=2), "pc-1", now)
    assert result == "ok"
    assert updates["machineCode"] is None
    assert updates["machineBoundAt"] is None
    assert updates["rebindCount"] == 3


def test_release_normalizes_machine_code():
    now = datetime.now(timezone.utc)
    # Bound with dashes/case; releasing PC presents a differently-formatted code.
    result, _ = core.decide_release(_license(machineCode="AB-cd-12"), "abcd12", now)
    assert result == "ok"


def test_release_from_other_machine_rejected():
    now = datetime.now(timezone.utc)
    assert core.decide_release(_license(machineCode="pc-1"), "pc-2", now)[0] == "wrong_machine"


def test_release_unbound_is_ok_and_noop():
    now = datetime.now(timezone.utc)
    result, updates = core.decide_release(_license(machineCode=None), "pc-1", now)
    assert result == "ok"
    assert updates is None


def test_release_invalid_missing_license():
    now = datetime.now(timezone.utc)
    assert core.decide_release(None, "pc-1", now)[0] == "invalid"


def test_release_ignores_status_and_expiry():
    # A disabled or lapsed license must still be releasable so it can be moved.
    now = datetime.now(timezone.utc)
    disabled = _license(machineCode="pc-1", status="disabled")
    assert core.decide_release(disabled, "pc-1", now)[0] == "ok"
    expired = _license(machineCode="pc-1", type="monthly",
                       expires=(now - timedelta(days=1)).isoformat())
    assert core.decide_release(expired, "pc-1", now)[0] == "ok"


# -- token compatibility with the app's verifier ----------------------------


def test_signed_token_verifies_with_app_and_carries_recheck():
    priv, pub = app_keys.generate_keypair()
    lic = _license(machineCode="3F9A-2C1B-7E04")
    payload = core.build_online_payload(lic, "3F9A-2C1B-7E04", NOW, token_ttl_days=10)
    token = core.sign_online_token(payload, priv)

    info = app_keys.verify_token(token, pub)  # the SHIPPED app's verifier
    assert info.email == "jane@example.com"
    assert info.matches_machine("3F9A-2C1B-7E04")
    assert info.recheck is not None
    # recheck is ~10 days out.
    recheck = datetime.fromisoformat(info.recheck)
    assert (recheck - NOW).days == 10


def test_tampered_online_token_rejected_by_app():
    priv, pub = app_keys.generate_keypair()
    token = core.sign_online_token(core.build_online_payload(_license(), "m", NOW), priv)
    with pytest.raises(app_keys.LicenseError):
        app_keys.verify_token(token[:-4] + "aaaa", pub)
