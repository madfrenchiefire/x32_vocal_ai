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

import json
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


# -- app-facing: activate / check (plain HTTP -- the desktop app has no
#    Firebase SDK, so these are on_request JSON endpoints, not callables) ----


def _json(body: dict, status: int = 200) -> https_fn.Response:
    return https_fn.Response(json.dumps(body), status=status, mimetype="application/json")


def _app_endpoint(req: https_fn.Request, decide) -> https_fn.Response:
    if req.method != "POST":
        return _json({"error": "method_not_allowed"}, 405)
    data = req.get_json(silent=True) or {}
    key = str(data.get("key", "")).strip()
    machine = str(data.get("machineCode", "")).strip()
    app_id = str(data.get("app", "")).strip()
    if not key or not machine or not app_id:
        return _json({"error": "missing_fields"}, 400)
    license = _get_license(key)
    if not core.product_matches(license, app_id):
        # Wrong product (or unknown key) -- don't leak which; report invalid.
        return _json({"error": "invalid"}, 200)
    result, updates = decide(license, machine, _now())
    if result != "ok":
        return _json({"error": result}, 200)
    _db().collection(LICENSES).document(key).update(updates)
    merged = {**license, **updates}  # type: ignore[dict-item]
    payload = core.build_online_payload(merged, machine, _now())
    return _json({"token": core.sign_online_token(payload, _private_key_hex())})


@https_fn.on_request(secrets=["LICENSE_PRIVATE_KEY"])
def activate(req: https_fn.Request) -> https_fn.Response:
    return _app_endpoint(req, core.decide_activation)


@https_fn.on_request(secrets=["LICENSE_PRIVATE_KEY"])
def check(req: https_fn.Request) -> https_fn.Response:
    return _app_endpoint(req, core.decide_check)


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
        "productId": data.get("productId", "x32-sonicsniper"),
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
