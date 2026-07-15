"""Embedded license-verification public key.

This is the PUBLIC half of the vendor's Ed25519 keypair -- safe to ship. It
verifies that a license token was signed by the matching private key (which
never leaves the vendor). Empty by default: run

    python -m app.tools.license_gen init

once to generate your keypair -- that writes the private key to
``secrets/`` (gitignored, never shipped) and fills in PUBLIC_KEY_HEX below.
Until then, the trial still works but no purchased key can be validated.
"""
from __future__ import annotations

PUBLIC_KEY_HEX = ""
