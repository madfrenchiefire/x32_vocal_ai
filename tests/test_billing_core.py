"""Tests for the Stripe billing logic (cloud/functions/billing_core.py) --
plain event dicts, no Stripe/Firebase needed."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

FUNCS = Path(__file__).resolve().parent.parent / "cloud" / "functions"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, FUNCS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load("license_core")  # billing_core imports `license_core`
import sys  # noqa: E402
sys.modules.setdefault("license_core", core)
billing = _load("billing_core")

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


def _checkout_session(**over):
    base = {
        "id": "cs_test_1",
        "customer": "cus_1",
        "subscription": None,
        "customer_details": {"email": "Jane@Example.com", "name": "Jane"},
        "metadata": {"productId": "x32-sonicsniper", "type": "lifetime"},
    }
    base.update(over)
    return base


def test_lifetime_checkout_creates_perpetual_license():
    lic = billing.license_from_checkout(_checkout_session(), NOW)
    assert lic is not None
    assert lic["ownerEmail"] == "jane@example.com"  # lowercased
    assert lic["productId"] == "x32-sonicsniper"
    assert lic["type"] == "lifetime"
    assert lic["expires"] is None
    assert lic["status"] == "active"
    assert lic["key"].startswith("XVAI-")
    assert lic["stripeSessionId"] == "cs_test_1"


def test_monthly_checkout_sets_expiry():
    session = _checkout_session(
        subscription="sub_1",
        metadata={"productId": "x32-sonicsniper", "type": "monthly"},
    )
    lic = billing.license_from_checkout(session, NOW)
    assert lic["type"] == "monthly"
    assert lic["expires"] is not None
    assert lic["expires"] > NOW.isoformat()
    assert lic["stripeSubscriptionId"] == "sub_1"


def test_checkout_without_email_returns_none():
    session = _checkout_session(customer_details={}, customer_email=None)
    assert billing.license_from_checkout(session, NOW) is None


def test_checkout_with_bad_type_returns_none():
    session = _checkout_session(metadata={"productId": "x", "type": "bogus"})
    assert billing.license_from_checkout(session, NOW) is None


def test_renewal_extends_and_reactivates():
    upd = billing.renewal_updates(NOW)
    assert upd["status"] == "active"
    assert upd["expires"] > NOW.isoformat()


def test_cancellation_disables():
    assert billing.cancellation_updates(NOW)["status"] == "disabled"


def test_only_cycle_invoices_are_renewals():
    assert billing.is_renewal_invoice({"billing_reason": "subscription_cycle"}) is True
    # The first invoice of a new subscription must NOT re-extend (checkout
    # already created the license).
    assert billing.is_renewal_invoice({"billing_reason": "subscription_create"}) is False
