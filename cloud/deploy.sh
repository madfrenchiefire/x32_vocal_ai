#!/usr/bin/env bash
# One-command deploy for the Simple Computers 101 license backend.
# Preflight-checks the things that break a deploy, then ships rules +
# functions + hosting. Run from anywhere; it cd's to its own dir (cloud/).
#
#   ./deploy.sh
#
# First time only (interactive, once): see the "MISSING" hints this prints,
# and cloud/README.md for the Stripe/secret setup.
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "ERROR: $*" >&2; exit 1; }

command -v firebase >/dev/null 2>&1 || fail "firebase CLI not found. Install:  npm i -g firebase-tools"
[ -f .firebaserc ] || fail "no Firebase project selected. Run once:  firebase use --add"

if grep -q "REPLACE_ME" hosting/firebase-config.js; then
  fail "hosting/firebase-config.js still has REPLACE_ME placeholders — fill it from the Firebase console (Project settings > Web app)."
fi

# Signing keypair present? (public_key.py is empty until license_gen init.)
python - <<'PY' || fail "no signing keypair yet. From the repo root run:  python -m app.tools.license_gen init"
import re, sys, pathlib
t = pathlib.Path("../app/licensing/public_key.py").read_text()
sys.exit(0 if re.search(r'PUBLIC_KEY_HEX = "[0-9a-f]', t) else 1)
PY

echo "Deploying Firestore rules + Cloud Functions + Hosting…"
firebase deploy --only firestore:rules,functions,hosting

cat <<'NOTE'

=== Deployed. First-time-only follow-ups (each is a one-time step) ===
  1. Secrets (functions won't run until these exist):
       firebase functions:secrets:set LICENSE_PRIVATE_KEY --data-file ../secrets/license_private_key.hex
     Selling via Stripe? also:
       firebase functions:secrets:set STRIPE_SECRET_KEY
       firebase functions:secrets:set STRIPE_WEBHOOK_SECRET
     (then re-run ./deploy.sh so the functions pick up the secrets)
  2. Stripe webhook: in the Stripe dashboard add an endpoint at the deployed
     stripe_webhook URL (events: checkout.session.completed, invoice.paid,
     customer.subscription.deleted); put its signing secret in
     STRIPE_WEBHOOK_SECRET above.
  3. Make yourself admin:  python set_admin.py you@example.com
NOTE
