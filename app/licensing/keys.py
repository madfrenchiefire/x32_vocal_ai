"""License token crypto + machine fingerprint.

Token format (a single pasteable string):

    X32SNIPER1.<base64url(payload_json)>.<base64url(ed25519_signature)>

`payload_json` is a compact JSON object the vendor signs with the private
key; the app verifies the signature with the embedded public key and then
checks the payload's own rules (expiry, machine lock). Fields:

    v        format version (int)
    id       license id (uuid hex) -- for the vendor's records / revocation
    name     licensee name
    email    licensee email ("" if none)
    tier     edition string (e.g. "pro")
    issued   ISO-8601 date the key was minted
    expires  ISO-8601 date, or null for a perpetual license
    machine  machine fingerprint this key is locked to, or null for a
             floating (any-PC) key

Everything in the payload is covered by the signature, so none of it can be
edited without invalidating the token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

TOKEN_PREFIX = "X32SNIPER1"
TOKEN_FORMAT_VERSION = 1


class LicenseError(Exception):
    """Raised for a malformed or cryptographically invalid token. A *valid*
    token that is merely expired or machine-mismatched does NOT raise --
    that's a status (see app.licensing.manager), not a parse error."""


# -- base64url helpers (no padding, URL-safe) -------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# -- keypair (used by the vendor's key-generator tool) ----------------------


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_hex, public_key_hex). The private hex stays with
    the vendor (never shipped); the public hex is embedded in the app
    (app.licensing.public_key)."""
    private = Ed25519PrivateKey.generate()
    private_hex = private.private_bytes_raw().hex()
    public_hex = private.public_key().public_bytes_raw().hex()
    return private_hex, public_hex


def public_key_hex_for_private(private_key_hex: str) -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private.public_key().public_bytes_raw().hex()


# -- license payload --------------------------------------------------------


@dataclass(frozen=True)
class LicenseInfo:
    id: str
    name: str
    email: str
    tier: str
    issued: str
    expires: str | None
    machine: str | None
    # Online model only (app.licensing is offline by default): the ISO
    # datetime by which the app should re-check with the license server.
    # None for a purely offline signed key. See cloud/ for the server that
    # issues these short-lived tokens.
    recheck: str | None = None
    # Product this license is for (e.g. "x32-sonicsniper"). One signing key
    # can cover many apps sold under the same vendor; each app verifies the
    # token's `app` matches its own product id, so a key for another product
    # can't unlock this one. None on a legacy/single-product token.
    app: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LicenseInfo":
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            email=str(payload.get("email", "")),
            tier=str(payload.get("tier", "")),
            issued=str(payload.get("issued", "")),
            expires=(payload["expires"] if payload.get("expires") else None),
            machine=(payload["machine"] if payload.get("machine") else None),
            recheck=(payload["recheck"] if payload.get("recheck") else None),
            app=(payload["app"] if payload.get("app") else None),
        )

    def matches_product(self, product_id: str) -> bool:
        """True if this token isn't product-scoped, or is for `product_id`."""
        return self.app is None or self.app == product_id

    def is_expired(self, today: date | None = None) -> bool:
        if not self.expires:
            return False
        today = today or datetime.now(timezone.utc).date()
        try:
            return date.fromisoformat(self.expires) < today
        except ValueError:
            # An unparseable expiry is treated as expired -- fail closed.
            return True

    def matches_machine(self, this_machine_code: str) -> bool:
        """True if this key is floating (not machine-locked) or locked to
        the given machine code. Locking uses the short machine *code* (what
        the licensee sends the vendor), normalized case/spacing-insensitively
        so a pasted value matches regardless of formatting."""
        if self.machine is None:
            return True
        return _normalize_machine(self.machine) == _normalize_machine(this_machine_code)


def build_payload(
    name: str,
    email: str = "",
    tier: str = "pro",
    expires: str | None = None,
    machine: str | None = None,
    license_id: str | None = None,
    issued: str | None = None,
) -> dict[str, Any]:
    return {
        "v": TOKEN_FORMAT_VERSION,
        "id": license_id or uuid.uuid4().hex,
        "name": name,
        "email": email,
        "tier": tier,
        "issued": issued or datetime.now(timezone.utc).date().isoformat(),
        "expires": expires,
        "machine": machine,
    }


# -- sign / verify ----------------------------------------------------------


def sign_token(payload: dict[str, Any], private_key_hex: str) -> str:
    """Serialize + sign a payload into a pasteable license token."""
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = private.sign(body)
    return f"{TOKEN_PREFIX}.{_b64encode(body)}.{_b64encode(signature)}"


def verify_token(token: str, public_key_hex: str) -> LicenseInfo:
    """Parse and cryptographically verify a token. Raises LicenseError if
    it's malformed, the prefix/version is wrong, or the signature does not
    verify against public_key_hex. Returns the LicenseInfo on success --
    expiry / machine-lock are the caller's checks, not this one's."""
    token = token.strip()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise LicenseError("not a valid license key (unexpected format)")
    _, body_b64, sig_b64 = parts
    try:
        body = _b64decode(body_b64)
        signature = _b64decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise LicenseError("license key is corrupt (bad encoding)") from exc

    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    try:
        public.verify(signature, body)
    except InvalidSignature as exc:
        raise LicenseError("license key signature is invalid (not issued by this vendor, or tampered)") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("license key payload is corrupt") from exc
    if payload.get("v") != TOKEN_FORMAT_VERSION:
        raise LicenseError(f"unsupported license format version {payload.get('v')!r}")
    return LicenseInfo.from_payload(payload)


# -- machine fingerprint ----------------------------------------------------


def _raw_machine_identifiers() -> list[str]:
    """Best-effort stable hardware/OS identifiers. Windows MachineGuid is
    the most stable on the real target; uuid.getnode() (MAC) and the
    platform tuple are the cross-platform fallback. Deliberately a *set* of
    inputs hashed together so one changing (e.g. a swapped NIC) still
    usually leaves enough overlap -- but note this is best-effort, not a
    TPM-backed hardware id."""
    identifiers = [platform.system(), platform.machine()]
    try:
        identifiers.append(format(uuid.getnode(), "x"))
    except Exception:
        pass
    # Windows: the registry MachineGuid is stable across reboots/NIC swaps.
    if platform.system() == "Windows":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                identifiers.append(str(guid))
        except Exception:
            pass
    return identifiers


def machine_fingerprint() -> str:
    """A stable per-machine hex string (sha256 of the identifiers)."""
    joined = "|".join(_raw_machine_identifiers()).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def _normalize_machine(value: str) -> str:
    return "".join(value.split()).replace("-", "").upper()


def machine_code(fingerprint: str | None = None) -> str:
    """Short, human-readable form of the fingerprint for the licensee to
    send to the vendor when requesting a machine-locked key. First 20 hex
    chars, grouped in 4s (e.g. '3F9A-2C1B-7E04-...')."""
    fp = fingerprint or machine_fingerprint()
    short = fp[:20].upper()
    return "-".join(short[i:i + 4] for i in range(0, len(short), 4))
