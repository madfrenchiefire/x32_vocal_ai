"""Stripe price mapping for the storefront.

Fill in the Stripe Price IDs for each (productId, plan) you sell. Create the
products/prices in the Stripe dashboard:
  - a recurring monthly price  -> plan "monthly"
  - a one-time price           -> plan "lifetime"

create_checkout_session looks the price up here, so adding a new app/plan is
just another entry.
"""
from __future__ import annotations

# (productId, plan) -> Stripe Price ID
PRICES: dict[tuple[str, str], str] = {
    ("x32-sonicsniper", "monthly"): "price_REPLACE_ME_MONTHLY",
    ("x32-sonicsniper", "lifetime"): "price_REPLACE_ME_LIFETIME",
}


def price_id(product_id: str, plan: str) -> str | None:
    return PRICES.get((product_id, plan))
