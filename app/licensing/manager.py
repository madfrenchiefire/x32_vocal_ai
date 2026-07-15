"""License evaluation: turn the stored token + trial state into a single
status the rest of the app gates on.

States (LicenseStatus.state):
    licensed         valid, signed, not expired, machine matches
    machine_mismatch valid signed key, but locked to a different PC
    expired_license  valid signed key, but past its expiry date
    trial            no valid key, but within the free trial window
    trial_expired    no valid key and the trial window has elapsed
    unlicensed       licensing not configured (no embedded public key)

`is_functional()` -- whether the app should run its real features -- is true
only for "licensed" and "trial".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.licensing.keys import (
    LicenseError,
    LicenseInfo,
    machine_code,
    machine_fingerprint,
    verify_token,
)
from app.licensing.public_key import PUBLIC_KEY_HEX
from app.licensing.store import LicenseStore

DEFAULT_TRIAL_DAYS = 14

_FUNCTIONAL_STATES = {"licensed", "trial"}


@dataclass
class LicenseStatus:
    state: str
    message: str
    machine_code: str
    licensee: str | None = None
    tier: str | None = None
    expires: str | None = None
    trial_days_remaining: int | None = None

    @property
    def functional(self) -> bool:
        return self.state in _FUNCTIONAL_STATES

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "functional": self.functional,
            "message": self.message,
            "machine_code": self.machine_code,
            "licensee": self.licensee,
            "tier": self.tier,
            "expires": self.expires,
            "trial_days_remaining": self.trial_days_remaining,
        }


class LicenseManager:
    def __init__(
        self,
        store: LicenseStore | None = None,
        public_key_hex: str | None = None,
        trial_days: int = DEFAULT_TRIAL_DAYS,
        fingerprint: str | None = None,
        product_id: str | None = None,
        now_provider=None,
    ) -> None:
        self.store = store or LicenseStore()
        self.public_key_hex = PUBLIC_KEY_HEX if public_key_hex is None else public_key_hex
        self.trial_days = trial_days
        self.fingerprint = fingerprint or machine_fingerprint()
        # When set, a token whose `app` names a different product is ignored
        # (falls through to trial) -- one signing key can cover many apps.
        self.product_id = product_id
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    # -- public API ----------------------------------------------------------

    def machine_code(self) -> str:
        return machine_code(self.fingerprint)

    def evaluate_at_startup(self, diagnostics=None) -> LicenseStatus:
        """status() plus a one-line log/print of the result -- for app
        startup. Returns the status so the caller can gate services on
        `.functional`."""
        status = self.status()
        if diagnostics is not None:
            diagnostics.log_state_change(
                "license_evaluated",
                after={"state": status.state, "tier": status.tier, "expires": status.expires,
                       "trial_days_remaining": status.trial_days_remaining},
            )
        print(f"License: {status.message}")
        return status

    def status(self) -> LicenseStatus:
        """Evaluate the current license/trial state. Starts the trial clock
        the first time it's called on a machine with no valid key."""
        token = self.store.load_token()
        if token and self.public_key_hex:
            resolved = self._status_from_token(token)
            if resolved is not None:
                return resolved
        return self._trial_status()

    def activate(self, token: str) -> LicenseStatus:
        """Validate and store a pasted token. Returns the resulting status;
        raises LicenseError if the token is malformed / wrong-signature, or
        the key is expired / for another machine (so the UI can show why)."""
        if not self.public_key_hex:
            raise LicenseError("licensing is not configured in this build (no embedded public key)")
        info = verify_token(token, self.public_key_hex)  # raises on bad signature/format
        if info.is_expired(self._now().date()):
            raise LicenseError(f"this license expired on {info.expires}")
        if not info.matches_machine(self.machine_code()):
            raise LicenseError(
                "this license is locked to a different computer -- request a key for machine code "
                f"{self.machine_code()}"
            )
        self.store.save_token(token)
        return self._licensed_status(info)

    def deactivate(self) -> None:
        self.store.clear_token()

    # -- internals -----------------------------------------------------------

    def _status_from_token(self, token: str) -> LicenseStatus | None:
        try:
            info = verify_token(token, self.public_key_hex)
        except LicenseError:
            return None  # fall through to trial -- a junk token isn't a license
        if self.product_id is not None and not info.matches_product(self.product_id):
            return None  # a token for another product isn't a license for this app
        if info.is_expired(self._now().date()):
            return LicenseStatus(
                state="expired_license",
                message=f"License expired on {info.expires}.",
                machine_code=self.machine_code(),
                licensee=info.name,
                tier=info.tier,
                expires=info.expires,
            )
        if not info.matches_machine(self.machine_code()):
            return LicenseStatus(
                state="machine_mismatch",
                message="This license is locked to a different computer.",
                machine_code=self.machine_code(),
                licensee=info.name,
                tier=info.tier,
            )
        # Online tokens carry a `recheck` horizon: the app must re-verify with
        # the server by then. Past it (and no successful refresh), the app is
        # not functional until it reconnects -- but note this is a *hard*
        # horizon well beyond the weekly cadence, so a short no-internet
        # stretch (a gig) never trips it.
        if info.recheck is not None and self._past_recheck(info.recheck):
            return LicenseStatus(
                state="recheck_required",
                message="Please connect to the internet to re-verify this license.",
                machine_code=self.machine_code(),
                licensee=info.name,
                tier=info.tier,
                expires=info.expires,
            )
        return self._licensed_status(info)

    def _past_recheck(self, recheck: str) -> bool:
        try:
            horizon = datetime.fromisoformat(recheck)
        except ValueError:
            return False  # unparseable -> don't lock on it
        if horizon.tzinfo is None:
            horizon = horizon.replace(tzinfo=timezone.utc)
        return self._now() >= horizon

    def _licensed_status(self, info: LicenseInfo) -> LicenseStatus:
        return LicenseStatus(
            state="licensed",
            message=f"Licensed to {info.name}." + (f" Expires {info.expires}." if info.expires else ""),
            machine_code=self.machine_code(),
            licensee=info.name,
            tier=info.tier,
            expires=info.expires,
        )

    def _trial_status(self) -> LicenseStatus:
        if not self.public_key_hex:
            # Licensing not set up in this build -- still allow the trial so a
            # fresh clone/build is usable, but say so.
            base_message = "Licensing not configured. "
        else:
            base_message = ""

        record = self.store.load_trial()
        now = self._now()
        # A trial file from a different machine doesn't count -- start fresh.
        if record is None or record.get("machine") != self.fingerprint:
            record = self.store.start_trial(self.fingerprint, now=now)

        try:
            first_run = datetime.fromisoformat(record["first_run"])
        except (KeyError, ValueError):
            record = self.store.start_trial(self.fingerprint, now=now)
            first_run = now

        # Clock-rollback guard: if the system clock is now earlier than the
        # last time we saw it, don't extend the trial -- measure from
        # first_run using the later of (now, last_seen).
        last_seen = _parse_dt(record.get("last_seen")) or first_run
        effective_now = max(now, last_seen)
        record["last_seen"] = effective_now.isoformat()
        self.store.save_trial(record)

        days_used = (effective_now - first_run).days
        remaining = self.trial_days - days_used
        if remaining <= 0:
            return LicenseStatus(
                state="trial_expired",
                message=base_message + f"Free trial ended ({self.trial_days} days). Enter a license key to continue.",
                machine_code=self.machine_code(),
                trial_days_remaining=0,
            )
        return LicenseStatus(
            state="trial",
            message=base_message + f"Free trial: {remaining} day(s) remaining.",
            machine_code=self.machine_code(),
            trial_days_remaining=remaining,
        )


def _parse_dt(text) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
