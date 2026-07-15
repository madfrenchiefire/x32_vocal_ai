"""Vendor-only license key generator. NOT shipped in the built .exe.

Workflow:

    # once, to create your signing keypair (writes the private key to
    # secrets/ -- gitignored, never distribute it -- and embeds the public
    # key in app/licensing/public_key.py):
    python -m app.tools.license_gen init

    # per sale -- floating key (any PC):
    python -m app.tools.license_gen issue --name "Jane Doe" --email jane@x.com

    # machine-locked key (the customer sends you the machine code the app
    # shows on its License screen):
    python -m app.tools.license_gen issue --name "Jane Doe" \\
        --machine 3F9A-2C1B-7E04-9D8E-1A2B

    # subscription (expires): --expires 2027-01-01  or  --expires +365d
    # verify a key you produced:
    python -m app.tools.license_gen verify --token X32VOCAL1.xxxx.yyyy

Sell the key however you like (Gumroad, Lemon Squeezy, Stripe...); this tool
just mints the signed token you deliver to the buyer.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.licensing.keys import (
    LicenseError,
    build_payload,
    generate_keypair,
    public_key_hex_for_private,
    sign_token,
    verify_token,
)

DEFAULT_PRIVATE_KEY_PATH = Path("secrets") / "license_private_key.hex"
PUBLIC_KEY_MODULE = Path("app") / "licensing" / "public_key.py"


def _parse_expires(value: str | None) -> str | None:
    """Accept an ISO date (YYYY-MM-DD), a relative '+Nd'/'+Nm'/'+Ny', or
    'never'/'' for a perpetual license."""
    if value is None or value.lower() in ("", "never", "none", "perpetual"):
        return None
    match = re.fullmatch(r"\+(\d+)([dmy])", value.strip(), re.IGNORECASE)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        days = amount * {"d": 1, "m": 30, "y": 365}[unit]
        return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise SystemExit(f"--expires must be YYYY-MM-DD, '+Nd/Nm/Ny', or 'never' (got {value!r})")


def _cmd_init(args: argparse.Namespace) -> int:
    private_path = Path(args.private_key)
    if private_path.exists() and not args.force:
        print(
            f"ERROR: {private_path} already exists. Re-running init makes a NEW keypair, which "
            "invalidates every key you've already issued. Pass --force only if you truly mean to.",
            file=sys.stderr,
        )
        return 1

    private_hex, public_hex = generate_keypair()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_text(private_hex, encoding="utf-8")
    try:
        private_path.chmod(0o600)
    except OSError:
        pass

    _write_public_key_module(public_hex)
    print(f"Private key written to {private_path} (KEEP THIS SECRET -- it is gitignored, never ship it).")
    print(f"Public key embedded in {PUBLIC_KEY_MODULE}.")
    print("You can now issue keys with `python -m app.tools.license_gen issue --name ...`.")
    return 0


def _write_public_key_module(public_hex: str) -> None:
    text = PUBLIC_KEY_MODULE.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'^PUBLIC_KEY_HEX = .*$', f'PUBLIC_KEY_HEX = "{public_hex}"', text, flags=re.MULTILINE
    )
    if count != 1:
        raise SystemExit(f"could not update PUBLIC_KEY_HEX in {PUBLIC_KEY_MODULE} (found {count} matches)")
    PUBLIC_KEY_MODULE.write_text(new_text, encoding="utf-8")


def _load_private_key(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit(
            f"private key not found at {path} -- run `python -m app.tools.license_gen init` first "
            "(or pass --private-key)."
        )


def _cmd_issue(args: argparse.Namespace) -> int:
    private_hex = _load_private_key(args.private_key)
    payload = build_payload(
        name=args.name,
        email=args.email or "",
        tier=args.tier,
        expires=_parse_expires(args.expires),
        machine=(args.machine or None),
    )
    token = sign_token(payload, private_hex)
    print(token)
    if args.machine:
        print(f"\n(locked to machine {args.machine})", file=sys.stderr)
    if payload["expires"]:
        print(f"(expires {payload['expires']})", file=sys.stderr)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.public_key:
        public_hex = args.public_key
    elif args.private_key and Path(args.private_key).exists():
        public_hex = public_key_hex_for_private(_load_private_key(args.private_key))
    else:
        from app.licensing.public_key import PUBLIC_KEY_HEX

        public_hex = PUBLIC_KEY_HEX
    if not public_hex:
        raise SystemExit("no public key available -- run init, or pass --public-key/--private-key")
    try:
        info = verify_token(args.token, public_hex)
    except LicenseError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"VALID. name={info.name!r} email={info.email!r} tier={info.tier!r} "
          f"issued={info.issued} expires={info.expires} machine={info.machine}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.license_gen",
        description="Vendor-only license key generator (never ship this).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="generate the signing keypair (run once)")
    p_init.add_argument("--private-key", default=str(DEFAULT_PRIVATE_KEY_PATH))
    p_init.add_argument("--force", action="store_true", help="overwrite an existing private key")
    p_init.set_defaults(func=_cmd_init)

    p_issue = sub.add_parser("issue", help="mint a signed license key")
    p_issue.add_argument("--name", required=True)
    p_issue.add_argument("--email", default="")
    p_issue.add_argument("--tier", default="pro")
    p_issue.add_argument("--expires", default=None, help="YYYY-MM-DD, '+Nd/Nm/Ny', or 'never' (default)")
    p_issue.add_argument("--machine", default=None, help="machine code to lock to (omit for a floating key)")
    p_issue.add_argument("--private-key", default=str(DEFAULT_PRIVATE_KEY_PATH))
    p_issue.set_defaults(func=_cmd_issue)

    p_verify = sub.add_parser("verify", help="verify a token you produced")
    p_verify.add_argument("--token", required=True)
    p_verify.add_argument("--public-key", default=None)
    p_verify.add_argument("--private-key", default=str(DEFAULT_PRIVATE_KEY_PATH))
    p_verify.set_defaults(func=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
