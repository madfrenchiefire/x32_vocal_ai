"""Firebase Cloud Functions (2nd gen, Python) for the license server.

Thin wrapper over license_core (the tested, Firebase-free logic). The app
calls `activate` and `check`; the portal calls `deactivate` (self-service
"move to a new PC") and, for admins, `admin_create` / `admin_update`.

Deploy: see cloud/README.md. The Ed25519 signing key lives in Secret
Manager (LICENSE_PRIVATE_KEY), never in this source or the shipped app.

Endpoints (HTTPS callable):
    activate({key, machineCode})   -> {token} | {error}
    check({key, machineCode})      -> {token} | {error}
    deactivate({key})              -> {ok}    | {error}   (auth required)
    admin_create({...})            -> {key}               (admin only)
    admin_update({key, ...})       -> {ok}                (admin only)
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from firebase_admin import firestore, initialize_app
from firebase_functions import https_fn, options

import license_core as core

initialize_app()
options.set_global_options(region="us-central1", max_instances=10)

LICENSES = "licenses"


def _db():
    return firestore.client()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _private_key_hex() -> str:
    key = os.environ.get("LICENSE_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("LICENSE_PRIVATE_KEY secret is not set")
    return key


def _get_license(key: str) -> dict | None:
    snap = _db().collection(LICENSES).document(key).get()
    return snap.to_dict() if snap.exists else None


# -- app-facing: activate / check -------------------------------------------


@https_fn.on_call(secrets=["LICENSE_PRIVATE_KEY"])
def activate(req: https_fn.CallableRequest) -> dict:
    key = (req.data or {}).get("key", "").strip()
    machine = (req.data or {}).get("machineCode", "").strip()
    if not key or not machine:
        return {"error": "missing_fields"}
    license = _get_license(key)
    result, updates = core.decide_activation(license, machine, _now())
    if result != "ok":
        return {"error": result}
    _db().collection(LICENSES).document(key).update(updates)
    license = {**license, **updates}  # type: ignore[dict-item]
    payload = core.build_online_payload(license, machine, _now())
    return {"token": core.sign_online_token(payload, _private_key_hex())}


@https_fn.on_call(secrets=["LICENSE_PRIVATE_KEY"])
def check(req: https_fn.CallableRequest) -> dict:
    key = (req.data or {}).get("key", "").strip()
    machine = (req.data or {}).get("machineCode", "").strip()
    if not key or not machine:
        return {"error": "missing_fields"}
    license = _get_license(key)
    result, updates = core.decide_check(license, machine, _now())
    if result != "ok":
        return {"error": result}
    _db().collection(LICENSES).document(key).update(updates)
    payload = core.build_online_payload(license, machine, _now())
    return {"token": core.sign_online_token(payload, _private_key_hex())}


# -- portal: self-service deactivate ----------------------------------------


@https_fn.on_call()
def deactivate(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        return {"error": "auth_required"}
    key = (req.data or {}).get("key", "").strip()
    license = _get_license(key)
    email = (req.auth.token.get("email") or "")
    is_admin = bool(req.auth.token.get("admin"))
    result, updates = core.decide_deactivation(license, email, is_admin)
    if result != "ok":
        return {"error": result}
    _db().collection(LICENSES).document(key).update(updates)
    return {"ok": True}


# -- admin: create / update -------------------------------------------------


def _require_admin(req: https_fn.CallableRequest) -> bool:
    return req.auth is not None and bool(req.auth.token.get("admin"))


@https_fn.on_call()
def admin_create(req: https_fn.CallableRequest) -> dict:
    if not _require_admin(req):
        return {"error": "forbidden"}
    data = req.data or {}
    owner_email = (data.get("ownerEmail") or "").strip().lower()
    lic_type = data.get("type", "lifetime")
    if not owner_email or lic_type not in ("monthly", "lifetime"):
        return {"error": "invalid_fields"}
    key = data.get("key") or _generate_key()
    doc = {
        "key": key,
        "ownerEmail": owner_email,
        "ownerName": data.get("ownerName", ""),
        "type": lic_type,
        "tier": data.get("tier", "pro"),
        "status": "active",
        "machineCode": None,
        "machineBoundAt": None,
        "rebindCount": 0,
        "expires": data.get("expires"),  # ISO string or None (lifetime)
        "createdAt": _now(),
        "updatedAt": _now(),
        "lastCheckAt": None,
        "note": data.get("note", ""),
    }
    _db().collection(LICENSES).document(key).set(doc)
    return {"key": key}


@https_fn.on_call()
def admin_update(req: https_fn.CallableRequest) -> dict:
    if not _require_admin(req):
        return {"error": "forbidden"}
    data = req.data or {}
    key = (data.get("key") or "").strip()
    if not key or _get_license(key) is None:
        return {"error": "invalid"}
    allowed = {k: data[k] for k in ("status", "expires", "type", "tier", "note", "ownerEmail") if k in data}
    if "machineCode" in data and data["machineCode"] in (None, ""):
        allowed["machineCode"] = None  # admin machine wipe
        allowed["machineBoundAt"] = None
    allowed["updatedAt"] = _now()
    _db().collection(LICENSES).document(key).update(allowed)
    return {"ok": True}


def _generate_key() -> str:
    """Human-ish key: XVAI-XXXX-XXXX-XXXX (base32, no ambiguous chars)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "XVAI-" + "-".join(groups)
