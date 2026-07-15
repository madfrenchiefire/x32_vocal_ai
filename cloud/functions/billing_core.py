"""Pure Stripe-billing logic -- no Firebase or Stripe SDK imports, so it
unit-tests with plain event dicts (cloud/functions/main.py is the thin
wrapper that verifies the Stripe signature and does the Firestore writes).

Turns Stripe events into license actions:

  checkout.session.completed  -> create a license (monthly or lifetime)
  invoice.paid (renewal)      -> extend a monthly license's expiry
  customer.subscription.deleted -> disable the license (cancelled)

Monthly vs lifetime and the product id ride in the checkout session's
`metadata` (set by create_checkout_session), so no Stripe price->product
table is needed on this side.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import license_core as core

# A few days past 30 so a slightly-late renewal webhook doesn't briefly lapse
# a paid subscriber.
MONTHLY_PERIOD_DAYS = 33


def _email_from_session(session: dict) -> str:
    details = session.get("customer_details") or {}
    return (details.get("email") or session.get("customer_email") or "").strip().lower()


def license_from_checkout(session: dict, now: datetime | None = None) -> dict | None:
    """Build the Firestore license doc for a completed checkout, or None if
    it's missing the fields we need (email / product / type)."""
    now = now or datetime.now(timezone.utc)
    email = _email_from_session(session)
    md = session.get("metadata") or {}
    product = (md.get("productId") or "").strip()
    lic_type = (md.get("type") or "").strip()
    if not email or not product or lic_type not in ("monthly", "lifetime"):
        return None

    expires = None
    if lic_type == "monthly":
        expires = (now + timedelta(days=MONTHLY_PERIOD_DAYS)).isoformat()

    return {
        "key": core.generate_key(),
        "ownerEmail": email,
        "ownerName": (session.get("customer_details") or {}).get("name", ""),
        "productId": product,
        "type": lic_type,
        "tier": md.get("tier", "pro"),
        "status": "active",
        "machineCode": None,
        "machineBoundAt": None,
        "rebindCount": 0,
        "expires": expires,
        "stripeCustomerId": session.get("customer"),
        "stripeSubscriptionId": session.get("subscription"),
        "stripeSessionId": session.get("id"),
        "createdAt": now,
        "updatedAt": now,
        "lastCheckAt": None,
        "note": "stripe checkout",
    }


def renewal_updates(now: datetime | None = None) -> dict:
    """Fields to write when a subscription renews (invoice.paid, cycle)."""
    now = now or datetime.now(timezone.utc)
    return {
        "status": "active",
        "expires": (now + timedelta(days=MONTHLY_PERIOD_DAYS)).isoformat(),
        "updatedAt": now,
    }


def cancellation_updates(now: datetime | None = None) -> dict:
    """Fields to write when a subscription is cancelled/ended."""
    now = now or datetime.now(timezone.utc)
    return {"status": "disabled", "updatedAt": now}


def is_renewal_invoice(invoice: dict) -> bool:
    """True only for recurring cycle invoices -- NOT the first invoice of a
    new subscription (that license was already created at checkout, so we
    must not double-extend it)."""
    return invoice.get("billing_reason") == "subscription_cycle"


def subscription_id_from_invoice(invoice: dict) -> str | None:
    return invoice.get("subscription")
