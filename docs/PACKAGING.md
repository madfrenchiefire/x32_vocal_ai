# Building the single-file `.exe` and selling license keys

This produces `dist/X32VocalAI.exe` — one double-clickable file that starts
the local server and opens the UI in the browser — plus the offline license
system used to sell it.

Everything here is done **on a Windows PC** (PyInstaller builds for the OS it
runs on; a Windows `.exe` must be built on Windows). Use Python 3.10–3.12 (the
`python-rtmidi` wheels only cover 3.8–3.12).

---

## One-time setup

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt pyinstaller
```

### 1. Generate your license signing keypair (once, ever)

```bat
python -m app.tools.license_gen init
```

This writes:
- `secrets\license_private_key.hex` — your **private** signing key. It is
  gitignored and must **never** be committed, shared, or bundled in the
  `.exe`. Back it up somewhere safe (a password manager). If you lose it you
  can't issue new keys that work with already-shipped builds; if it leaks,
  anyone can mint keys.
- `app\licensing\public_key.py` — the **public** half, embedded in the app so
  it can verify keys offline. This one is safe to commit and ship.

Re-running `init` makes a *new* keypair and invalidates every key you've
already sold — don't, unless you mean to (it warns you).

---

## Build the exe

```bat
pyinstaller x32vocal.spec
```

Result: `dist\X32VocalAI.exe`.

### The ASIO DLL step (important)

`sounddevice` bundles a PortAudio DLL that, in recent versions, is built
**without ASIO** (the same issue documented in `CLAUDE.md`). The 32-channel
card path needs ASIO. Two options:

- **Pin the older wheel before building:** `pip install "sounddevice==0.4.4"`
  (older wheels bundled ASIO), then `pyinstaller x32vocal.spec`. Simplest.
- **Swap the DLL:** replace the PortAudio DLL inside the venv's
  `sounddevice\_sounddevice_data\portaudio-binaries\` with an ASIO-enabled
  build *before* running PyInstaller (the spec collects whatever is there).

Verify after building: run the exe, open the UI → Device Setup → you should
see ASIO devices. If the list is empty, the bundled DLL still lacks ASIO.

### Shipping alongside the exe

Put a `config.json` next to `X32VocalAI.exe` if you want non-default settings
(e.g. `{"audio_block_size": 64}`). The app also writes per-user data
(license, trial state, logs) under `%LOCALAPPDATA%\X32VocalAI`, so those
survive reinstalls.

### Antivirus note

A `--onefile` PyInstaller exe self-extracts to a temp folder on launch, which
sometimes trips heuristic AV or SmartScreen on first run. Options: code-sign
the exe (an EV/OV certificate removes most warnings), or ship the one-folder
build instead (set nothing extra — just note it's a folder, not a single
file). Code-signing is the real fix if you distribute widely.

---

## Selling: issue keys per customer

Collect payment however you like (Gumroad, Lemon Squeezy, Stripe, Paddle…).
On each sale, mint a key and send it to the buyer:

```bat
:: Floating key (works on any PC):
python -m app.tools.license_gen issue --name "Jane Doe" --email jane@example.com

:: Machine-locked key (the buyer sends you the machine code the app shows on
:: its License screen):
python -m app.tools.license_gen issue --name "Jane Doe" --machine 3F9A-2C1B-7E04-9D8E-1A2B

:: Subscription that expires:
python -m app.tools.license_gen issue --name "Jane Doe" --expires 2027-01-01
python -m app.tools.license_gen issue --name "Jane Doe" --expires +365d
```

The command prints the license key (a single `X32VOCAL1.…` string). The buyer
pastes it into the app's **License** screen (click the license badge top-right,
or it appears automatically when the trial ends).

Verify a key you produced:

```bat
python -m app.tools.license_gen verify --token X32VOCAL1.xxxx.yyyy
```

---

## How the licensing behaves (what your customers see)

- **First run:** a **14-day free trial** starts automatically (full features).
  The badge shows "Trial: N d".
- **Trial ends with no key:** the app still launches but the functional API is
  blocked and a License screen asks for a key. The console services (OSC/MIDI/
  audio) don't start until a valid key is entered and the app is restarted.
- **Valid key entered:** unlocked. A machine-locked key only works on the PC
  matching its machine code; a floating key works anywhere.
- **Offline:** everything above works with no internet — keys are verified
  locally against the embedded public key.

**Honest limit:** no client-side license scheme is uncrackable. This deters
casual sharing (signed keys can't be forged or edited, machine-locked keys
don't work on another PC), not a determined reverse-engineer. Code-signing +
machine-locking is a proportionate bar for a niche pro-audio tool.

The trial-reset resistance is likewise modest: the trial timestamp lives in
`%LOCALAPPDATA%` and a clock-rollback is detected, but deleting that folder
resets the clock — the same limit every offline trial has.
