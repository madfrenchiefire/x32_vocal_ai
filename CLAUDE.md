# X32 AI Feedback Suppression — Project Spec

Real-time microphone feedback suppression for the Behringer X32, running as a locally
hosted app on a PC connected via the X-USB card (audio + MIDI) and Ethernet (OSC).
Web-based UI (Flask + WebSockets), consistent with the existing X32 Monitor Manager app.

## Core design principles

1. **The AI is never in the audio path.** The real-time audio path is biquad notch
   filters only (near-zero latency). FFT analysis and ML classification run on a
   parallel analysis thread that instructs the filter bank asynchronously.
2. **Snapshot before touching anything.** Every console state the app modifies
   (routing, assign sets, scribble strips/colors) is read and stored first, and is
   restorable — per channel, globally, and automatically on crash (watchdog).
3. **Gig-safe defaults.** Any failure mode must resolve to "mics on their original
   patch, console behaving stock" within ~1 second.
4. **Set C of the assign section is off-limits.** The app only provisions Sets A and B.

## Architecture — three services, one Flask/WebSocket backend

### 1. Audio engine
- `sounddevice` (PortAudio) with the Behringer X-USB ASIO driver, 48 kHz,
  64–128 sample buffer. Target total round trip ≤ ~10 ms; measure it (loopback click test).
- Audio callback does ONLY per-channel biquad notch filtering
  (`scipy.signal` SOS with persistent state, preallocated buffers, no allocation in callback).
- Analysis thread consumes a ring buffer: FFT peak detection heuristics
  (peak-to-average ratio, absence of harmonic structure, sustained growth) flag
  candidate frequencies; ML classifier confirms/vetoes; filter bank places notches.
- ML: small CNN on mel-spectrogram patches, "feedback vs musical content."
  Train in PyTorch, export ONNX, infer with `onnxruntime` on CPU (<1 ms). Fully local.
- Training data plan: deliberately ring out rooms at low PA level across mics/positions
  (positives); multitrack vocals/instruments incl. sustained notes, whistles, cymbal
  swells (negatives / false-positive hard cases).

### 2. OSC service (console control, UDP 10023)
- `python-osc`. Send `/xremote` and refresh every ~8 s to receive state changes.
  Subscribe to `/meters` (binary blobs) for live channel meters.
- On connect: query `/xinfo`, require firmware 4.0+ (User In/Out routing).
- **Routing automation** (the "Apply" button):
  1. Snapshot: `/config/routing/IN/*` blocks, all 32 `/config/userrout/in/NN`,
     CARD output blocks, `/config/userrout/out/NN`. Store as named JSON snapshot.
  2. For each selected channel, set its `userrout/in` entry to the matching Card return.
     CRITICAL: input routing switches in blocks of 8 — all non-selected channels in an
     affected block must have their userrout entries set to mirror their original
     sources (known from the snapshot) so they are unaffected.
  3. Use `userrout/out` + CARD-block = User Out to cherry-pick arbitrary selected
     channels' preamps onto Card outs 1–8 (selection can be scattered across blocks).
  4. Flip block routing to User In / User Out last, after everything is staged.
  - Pace writes (a few ms between messages, UDP); read back key values to confirm
    before reporting success.
- **Per-channel bypass/restore** = rewrite that one channel's `userrout/in` entry back
  to its snapshot value (single message; block stays in User mode; other channels
  unaffected). Implemented as a toggle (bypass ↔ re-insert).
- **Full restore** = replay the snapshot (web UI + crash watchdog; no physical button).
- **Console feedback**: write channel scribble-strip colors/names to show per-channel
  state (inserted vs bypassed, AI active/suppressing). Restore names/colors on disengage
  (they're in the snapshot).
- **Assign-set provisioning**: `/config/ctrl/A|B/enc|btn/N` — read/store existing Set A/B
  assignments first, then write MIDI-type assignments. Exact string encoding of MIDI
  assignments: verify against the Patrick-Gilles Maillot unofficial X32 OSC document,
  or empirically (assign one on the desk, query the parameter, copy the format).

### 3. MIDI service
- MIDI rides the X-USB card (enable "MIDI via X-USB card" in Setup → MIDI). Use `mido`
  + `python-rtmidi` on a background thread; fully isolated from the audio callback.
- One dedicated MIDI channel (default 16, configurable). Buttons = CC toggle (127/0),
  encoders = absolute CC 0–127.
- **Slot model** (8 channel slots max on hardware; slots stay stable for the session):

  | Set | Enc 1–4 | Btn 1–4 | Btn 5–8 |
  |-----|---------|---------|---------|
  | A | Sensitivity, slots 1–4 (CC 11–14) | AI on/off, slots 1–4 (CC 1–4) | Insert/bypass, slots 1–4 |
  | B | Sensitivity, slots 5–8 (CC 15–18) | AI on/off, slots 5–8 (CC 5–8) | Insert/bypass, slots 5–8 |

  (Insert/bypass CC numbers TBD — pick a clean contiguous range, e.g. CC 21–28.)
- Selecting a channel grabs the lowest free slot and provisions its controls via OSC;
  deselecting frees the slot. Channels beyond 8 may still be processed but are
  app-controlled only (configurable: allow or hard-cap at 8).
- DECIDED AGAINST: using the Remote/DAW button as system on/off (mode side effects on
  the fader surface). System arm/disarm lives in the web UI only. Do not revisit.

## Web UI (Flask + WebSockets)
- Routing panel: 32-channel selection grid (pull real channel names/colors via OSC),
  snapshot status, Save snapshot / Apply routing / Restore buttons, per-channel rows
  showing card-out slot, active notch count, AI toggle, assigned hardware controls.
- Per channel: detection sensitivity, max simultaneous notches (default 12), notch
  depth (−6 to −18 dB), Q/width, deploy speed.
- Modes: **Ring-out/setup** (aggressive, locks filters) vs **Live** (conservative,
  floating filters in reserve, slow release of unused notches).
- Global: bypass, ML confidence threshold slider (trust model vs pure heuristics),
  spectrum display per channel, event log (every notch: channel, frequency, time).

## Build phases
1. **Plumbing**: ASIO passthrough Card 1–4 → app → Card 1–4, latency measurement.
2. **OSC + MIDI control service**: connect, snapshot, apply/restore routing,
   per-channel bypass, Set A/B provisioning, MIDI listener, scribble-strip feedback.
   (Testable with console only, no audio engine.)
3. **Heuristic detection + notch filter bank** — usable product on its own.
4. **ML classifier** layered on top of heuristics.
5. Watchdog, event log, polish.

## Open items to verify (do not assume)
- Exact `/config/userrout` value maps (Local/AES50/Card ranges) — Maillot OSC doc.
- MIDI-assignment string format for `/config/ctrl/*` — Maillot doc or empirical.
- `/meters` blob layout for the meters we need.
- Achievable ASIO buffer size / measured round-trip latency on the target PC.
