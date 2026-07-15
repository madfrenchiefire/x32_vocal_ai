@echo off
REM One-command deploy for the Simple Computers 101 license backend (Windows).
REM Preflight-checks, then ships rules + functions + hosting. See cloud\README.md
REM for the one-time Stripe/secret setup.
setlocal
cd /d "%~dp0"

where firebase >nul 2>&1
if errorlevel 1 (
  echo ERROR: firebase CLI not found. Install:  npm i -g firebase-tools
  exit /b 1
)
if not exist ".firebaserc" (
  echo ERROR: no Firebase project selected. Run once:  firebase use --add
  exit /b 1
)
findstr /C:"REPLACE_ME" hosting\firebase-config.js >nul
if not errorlevel 1 (
  echo ERROR: hosting\firebase-config.js still has REPLACE_ME placeholders.
  echo        Fill it from the Firebase console ^(Project settings ^> Web app^).
  exit /b 1
)
python -c "import re,sys,pathlib; t=pathlib.Path('..','app','licensing','public_key.py').read_text(); sys.exit(0 if re.search(r'PUBLIC_KEY_HEX = \"[0-9a-f]', t) else 1)"
if errorlevel 1 (
  echo ERROR: no signing keypair yet. From the repo root run:
  echo        python -m app.tools.license_gen init
  exit /b 1
)

echo Deploying Firestore rules + Cloud Functions + Hosting...
call firebase deploy --only firestore:rules,functions,hosting
if errorlevel 1 ( echo Deploy failed. & exit /b 1 )

echo.
echo === Deployed. First-time-only follow-ups ===
echo   1. Secrets (functions won't run until these exist):
echo        firebase functions:secrets:set LICENSE_PRIVATE_KEY --data-file ..\secrets\license_private_key.hex
echo      Selling via Stripe? also STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET, then re-run deploy.bat.
echo   2. Stripe webhook: add the deployed stripe_webhook URL in the Stripe dashboard
echo      (events: checkout.session.completed, invoice.paid, customer.subscription.deleted).
echo   3. Make yourself admin:  python set_admin.py you@example.com
endlocal
