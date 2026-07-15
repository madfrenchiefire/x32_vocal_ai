# Online licensing backend (Firebase)

Server-authoritative, node-locked licensing with a weekly re-check and a
self-service customer portal. This directory holds everything that runs in
**your** Firebase project; the desktop app only calls the Cloud Functions and
verifies their signed tokens offline.

**Status:** stage 1 (schema, rules, Cloud Functions logic) is here and
unit-tested (`tests/test_license_core.py`). The app-side client (weekly
re-check) and the Hosting portal are the next stages.

## Locked design choices

- Token lifetime **~10 days**, app re-checks **weekly** → a disabled license
  stops working within ~a week. Fail-safe: if the server is merely
  *unreachable*, the app keeps running on its cached token (+ a short grace)
  and only hard-locks on an explicit `disabled`/`expired`/`wrong_machine`.
- Two license **types**: `monthly` (has `expires`, extended by billing) and
  `lifetime` (`expires: null`, killable only via `status`).
- Customers **self-serve machine moves** ("move to a new PC" releases the
  binding); you keep an admin override.

## Pieces

| Firebase product | Role |
|---|---|
| **Firestore** | `licenses` + `users` collections (the DB) |
| **Cloud Functions** | app API (`activate`/`check`) + portal actions (`deactivate`, `admin_*`); signs tokens |
| **Authentication** | email/password accounts (customers + admin) |
| **Hosting** | the self-service portal page (stage 4) |

The app never touches Firestore directly — only the Cloud Functions do
(Admin SDK). The portal reads licenses directly but can only **read**, and
only its owner's rows (see `firestore.rules`).

## Data model

`licenses/{key}` (doc id = the license key):

```
key           string   e.g. "XVAI-7F3A-9K2M-QP4T"
ownerEmail    string   lowercased; links the license to a customer account
ownerName     string
productId     string   which app this license is for (e.g. "x32-sonicsniper")
type          string   "monthly" | "lifetime"
tier          string   "pro"
status        string   "active" | "disabled"
machineCode   string?  bound machine (null = unbound)
machineBoundAt timestamp?
rebindCount   int      how many times the machine was released (abuse signal)
expires       timestamp?   null for lifetime
createdAt / updatedAt / lastCheckAt   timestamps
note          string   admin-only
```

`users/{uid}`: `{ email, admin: bool, createdAt }`. Admin is also a **custom
auth claim** (`admin: true`) — that's what the rules and functions check.

## API contract

`activate`/`check` are plain **HTTP POST** endpoints (the desktop app has no
Firebase SDK); `deactivate`/`admin_*` are **callable** functions (the web
portal, which needs the auth context).

| Function | Kind | Caller | In | Out |
|---|---|---|---|---|
| `activate` | HTTP | app | `{app, key, machineCode}` | `{token}` or `{error}` |
| `check` | HTTP | app | `{app, key, machineCode}` | `{token}` or `{error}` |
| `deactivate` | callable | portal (auth) | `{key}` | `{ok}` or `{error}` |
| `admin_create` | callable | portal (admin) | `{ownerEmail, productId, type, tier?, expires?, ownerName?}` | `{key}` |
| `admin_update` | callable | portal (admin) | `{key, status?/expires?/machineCode?/…}` | `{ok}` |

`activate`/`check` reject a key whose `productId` doesn't match the requesting
`app` (reported as `invalid`, so it doesn't leak product membership).

`error` values: `invalid`, `disabled`, `expired`, `wrong_machine`,
`forbidden`, `missing_fields`. The app treats `disabled`/`expired`/
`wrong_machine` as authoritative (lock); a network failure is NOT one of
these, so the app stays running on its cached token.

`token` is an `X32SNIPER1.…` Ed25519 token the app verifies offline with the
embedded public key — the same format `app/licensing` already validates, plus
a `recheck` datetime telling the app when to phone home next.

## Deploy (you do this once)

Prereqs: a Google account, the Firebase CLI (`npm i -g firebase-tools`), and
the **Blaze** (pay-as-you-go) plan on the project — Cloud Functions require it
(usage is within the free allowances at low volume, but a card must be on
file).

**One-time setup:**

```bash
cd cloud
firebase login
firebase use --add        # select your Firebase project (writes .firebaserc)
# fill hosting/firebase-config.js with your web app config (Project settings > Web app)
python -m app.tools.license_gen init    # from the repo root: keypair + embed public key
```

**Deploy — one command** (`deploy.sh` on macOS/Linux/Git-Bash, `deploy.bat` on
Windows cmd). It preflight-checks the above, then ships rules + functions +
hosting, and prints the first-time secret/webhook/admin steps:

```bash
./deploy.sh          # or:  deploy.bat
```

`firebase.json` already wires Firestore rules, the Python Functions
(`functions/`), and Hosting together — no `firebase init` needed.

**Secrets** (once; functions won't run until these exist), then re-deploy:

```bash
firebase functions:secrets:set LICENSE_PRIVATE_KEY --data-file ../secrets/license_private_key.hex
# selling via Stripe? also STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET
```

**Make yourself admin** (after signing up in the portal):

```bash
python set_admin.py you@example.com
```

## The portal (`hosting/`) — "Simple Computers 101 License Portal"

A plain HTML/JS page (no build step) served by Firebase Hosting. It manages
licenses across **all** the apps you sell (each license has a `productId`);
X32 SonicSniper is the first.

- **Customers** sign in (email/password, verified), see every license issued
  to their email (labeled by product), copy a key, and press **"Move to a new
  PC"** to release the machine binding themselves (calls `deactivate`).
- **Admins** (custom claim `admin: true`) get an extra panel: pick a
  **product**, create a license (monthly/lifetime) attached to a customer
  email, disable/enable, wipe a machine binding, and set expiry. Add new apps
  by adding an `<option>` to the Product dropdown in `hosting/index.html`.

`hosting/firebase-config.js` holds the (public, non-secret) web config you
paste from the Firebase console. To make yourself admin once:

```python
from firebase_admin import auth, initialize_app
initialize_app()
auth.set_custom_user_claims(auth.get_user_by_email("you@you.com").uid, {"admin": True})
```

(Sign out/in afterward so the new claim is in your token.)

## Storefront & billing (Stripe)

The portal has a **Buy** area (visible logged-out); purchases auto-create /
renew licenses via a Stripe webhook — no manual key issuing.

Flow: portal Buy button → `create_checkout_session` (HTTP) → Stripe-hosted
Checkout → on payment, Stripe calls `stripe_webhook`, which creates the
license (product/plan ride in the session `metadata`). Monthly renewals
(`invoice.paid`, cycle) extend the license's expiry; cancellation
(`customer.subscription.deleted`) disables it. The buyer then signs into the
portal with the email they paid with and sees their key.

Setup:
1. In **Stripe**, create a product with two prices: a recurring **monthly**
   price and a one-time **lifetime** price. Put their price IDs in
   `functions/pricing.py`.
2. Set secrets and the portal URL:
   ```bash
   firebase functions:secrets:set STRIPE_SECRET_KEY        # sk_live_… (or sk_test_…)
   firebase functions:secrets:set STRIPE_WEBHOOK_SECRET     # from the webhook, step 4
   # PORTAL_URL is used for the checkout success/cancel redirects:
   firebase functions:config unset  # (n/a) -- set PORTAL_URL as a function env var
   ```
   Set `PORTAL_URL` (e.g. `https://<project>.web.app`) as an env var on the
   `create_checkout_session` function (via `firebase.json` `environmentVariables`
   or the console).
3. Fill `hosting/firebase-config.js`: `functionsBaseUrl`, and the `storeProducts`
   prices/labels shown on the storefront.
4. Deploy, then in the **Stripe dashboard → Webhooks**, add an endpoint
   pointing at the deployed `stripe_webhook` URL, subscribed to
   `checkout.session.completed`, `invoice.paid`, and
   `customer.subscription.deleted`. Copy its signing secret into
   `STRIPE_WEBHOOK_SECRET` (step 2) and redeploy.
5. Test with Stripe **test mode** keys + a `4242…` test card before going live.

The webhook is idempotent on the checkout session id (Stripe retries won't
double-create), and verifies every event's Stripe signature.

## What the app will do (stage 3)

Replace the current offline `activate` with a call to the `activate` function
(sending the machine code), store the returned token, and re-check weekly via
`check`. Verification stays local (the embedded public key). If `check`
returns `disabled`/`expired`/`wrong_machine`, the app locks and shows the
License screen; if it just can't reach the server, it keeps working until the
cached token's `recheck` + grace elapses.

## Security notes

- The signing **private key** lives only in Secret Manager
  (`LICENSE_PRIVATE_KEY`) — never in this source, the app, or git.
- Firestore rules isolate customers to their own rows and forbid all client
  writes; every mutation is a Cloud Function running as admin.
- Require **verified email** before a customer can see licenses (the rules
  check `email_verified`), so nobody claims an address they don't own.
- Honest limit (unchanged): the desktop client can still be patched by a
  determined cracker. This model buys you revocation, machine control, and a
  real customer portal — not uncrackability.
```
