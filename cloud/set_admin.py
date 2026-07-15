"""Grant (or revoke) the admin custom claim on a portal account.

    python set_admin.py you@example.com          # make admin
    python set_admin.py someone@x.com --revoke   # remove admin

Needs the Firebase Admin SDK and credentials for your project:
  pip install firebase-admin
  # then either:
  gcloud auth application-default login
  # or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key JSON
  # (Firebase console > Project settings > Service accounts > Generate key).

The account must already exist (the person signs up in the portal first).
After running this, they must sign out and back in for the new claim to take
effect in their token.
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="set_admin.py")
    parser.add_argument("email")
    parser.add_argument("--revoke", action="store_true", help="remove admin instead of granting it")
    args = parser.parse_args(argv)

    try:
        from firebase_admin import auth, initialize_app
    except ImportError:
        print("firebase-admin not installed:  pip install firebase-admin", file=sys.stderr)
        return 1

    try:
        initialize_app()
    except Exception as exc:  # missing credentials, etc.
        print(f"Could not init Firebase Admin (credentials?): {exc}", file=sys.stderr)
        print("Run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS.", file=sys.stderr)
        return 1

    try:
        user = auth.get_user_by_email(args.email)
    except Exception as exc:
        print(f"No account for {args.email!r} (have them sign up in the portal first): {exc}", file=sys.stderr)
        return 1

    auth.set_custom_user_claims(user.uid, {"admin": not args.revoke})
    verb = "removed from" if args.revoke else "granted to"
    print(f"admin {verb} {args.email}. They must sign out/in for it to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
