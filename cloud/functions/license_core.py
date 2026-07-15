"""Pure license logic for the Cloud Functions -- no Firebase imports, so it
unit-tests without a live project (cloud/functions/main.py is the thin
Firebase wrapper that calls these).

Tokens are Ed25519-signed and format-compatible with the app's verifier
(app/licensing/keys.py verify_token): same X32SNIPER1.<body>.<sig> shape.
The online model adds a `recheck` field -- the ISO datetime by which the app
must phone home again -- so a disabled license stops working within about a
token lifetime even though the key itself never expires (lifetime licenses).

Decision helpers return (result, updates) where result is one of:
    "ok"            proceed; `updates` is the Firestore fields to write
    "invalid"       no such key
    "disabled"      status != active (admin revoked it)
    "expired"       past its expiry (lapsed monthly)
    "wrong_machine" locked to a different PC (needs release/admin wipe)
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TOKEN_PREFIX = "X32SNIPER1"
TOKEN_FORMAT_VERSION = 1
DEFAULT_TOKEN_TTL_DAYS = 10  # app rechecks weekly; ~10d covers a missed week + a no-wifi gig

STATUS_ACTIVE = "active"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _normalize_machine(value: str) -> str:
    return "".join((value or "").split()).replace("-", "").upper()


def _as_dt(value: Any) -> datetime | None:
    """Accept ISO strings or datetimes (Firestore returns datetimes)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# -- token signing ----------------------------------------------------------


def build_online_payload(license: dict, machine_code: str, now: datetime, token_ttl_days: int = DEFAULT_TOKEN_TTL_DAYS) -> dict:
    expires = _as_dt(license.get("expires"))
    return {
        "v": TOKEN_FORMAT_VERSION,
        "id": str(license.get("key", "")),
        "name": str(license.get("ownerName", license.get("ownerEmail", ""))),
        "email": str(license.get("ownerEmail", "")),
        "tier": str(license.get("tier", license.get("type", "pro"))),
        "issued": now.date().isoformat(),
        "expires": (expires.date().isoformat() if expires else None),
        "machine": machine_code,
        "recheck": (now + timedelta(days=token_ttl_days)).isoformat(),
    }


def sign_online_token(payload: dict, private_key_hex: str) -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private.sign(body)
    return f"{TOKEN_PREFIX}.{_b64encode(body)}.{_b64encode(signature)}"


# -- decisions --------------------------------------------------------------


def _base_validity(license: dict | None, now: datetime) -> str | None:
    """Shared status/expiry checks. Returns an error result, or None if OK."""
    if license is None:
        return "invalid"
    if license.get("status") != STATUS_ACTIVE:
        return "disabled"
    expires = _as_dt(license.get("expires"))
    if expires is not None and expires < now:
        return "expired"
    return None


def decide_activation(license: dict | None, machine_code: str, now: datetime) -> tuple[str, dict | None]:
    """First activation OR re-activation. Binds the machine if the license is
    unbound; rejects if it's bound to a different one."""
    err = _base_validity(license, now)
    if err:
        return err, None
    assert license is not None
    bound = license.get("machineCode")
    updates: dict = {"lastCheckAt": now}
    if bound:
        if _normalize_machine(bound) != _normalize_machine(machine_code):
            return "wrong_machine", None
    else:
        updates["machineCode"] = machine_code
        updates["machineBoundAt"] = now
    return "ok", updates


def decide_check(license: dict | None, machine_code: str, now: datetime) -> tuple[str, dict | None]:
    """Periodic re-check. Never binds -- the machine must already match."""
    err = _base_validity(license, now)
    if err:
        return err, None
    assert license is not None
    bound = license.get("machineCode")
    if not bound or _normalize_machine(bound) != _normalize_machine(machine_code):
        return "wrong_machine", None
    return "ok", {"lastCheckAt": now}


def decide_deactivation(license: dict | None, requester_email: str, is_admin: bool) -> tuple[str, dict | None]:
    """Release the machine binding (self-service 'move to a new PC', or admin
    wipe). Requester must own the license or be admin."""
    if license is None:
        return "invalid", None
    if not is_admin and _normalize_email(license.get("ownerEmail", "")) != _normalize_email(requester_email):
        return "forbidden", None
    return "ok", {
        "machineCode": None,
        "machineBoundAt": None,
        "rebindCount": int(license.get("rebindCount", 0)) + 1,
    }


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()
