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
- Exact `/config/routing/IN/*` and CARD-output block address strings. The
  per-channel `/config/userrout/in/NN` and `/config/userrout/out/NN` addresses
  (channels 1–32) are confirmed and implemented. The block-level routing
  addresses are best-effort placeholders in `app/osc/addresses.py`
  (`ROUTING_IN_BLOCKS_TODO_VERIFY`, `CARD_OUT_BLOCKS_TODO_VERIFY`,
  `ROUTING_ADDRESSES_VERIFIED = False`) — read-only queries only, never used
  to drive writes. Every `RoutingSnapshot` carries `routing_addresses_verified`
  so callers can tell confirmed data from placeholder data. Confirm against
  the Maillot doc or empirically, then flip `ROUTING_ADDRESSES_VERIFIED`.

## Diagnostics event schema

Every module (OSC, MIDI, audio, web) logs through
`app.diagnostics.logger.DiagnosticsLogger` — **never `print()`**. This is not
optional scaffolding; it's how a problem reported after a gig gets diagnosed
without access to the running system.

### On-disk format

One JSON object per line (JSONL), written to `logs/<session_name>.jsonl`
(append-only) and simultaneously kept in an in-memory ring buffer (default
last 10,000 events, `AppConfig.ring_buffer_size`). Every event:

```json
{
  "seq": 42,
  "timestamp": "2026-07-06T16:21:58.831Z",
  "monotonic": 1234.567,
  "category": "osc_tx",
  "correlation_id": "a1b2c3d4e5f6...",
  "payload": { "...": "category-specific, see below" }
}
```

- **`timestamp`** — ISO-8601 UTC, millisecond precision.
- **`monotonic`** — `time.monotonic()` at log time; use this (not
  `timestamp`) for measuring durations, since wall-clock time can jump.
- **`category`** — one of: `osc_tx`, `osc_rx`, `midi_rx`, `user_action`,
  `state_change`, `watchdog`, `error`.
- **`correlation_id`** — ties a user action to everything it caused. See
  below.
- **`payload`** — raw data, exactly as sent/received; never a decoded
  summary in place of the raw bytes.

### Payload shape per category

- `osc_tx` / `osc_rx`: `{"address": "/config/userrout/in/01", "args": [5]}`
  — the exact OSC address and argument list, no interpretation.
- `midi_rx`: `{"raw_bytes": [176, 1, 127], "parsed": {...}}` — raw MIDI
  bytes plus whatever meaning was decoded from them.
- `user_action`: `{"action": "export_debug_bundle", "details": {...}}` —
  logged first, and its `correlation_id` is what gets attached everywhere
  else. `DiagnosticsLogger.log_user_action(...)` returns the id to reuse.
- `state_change`: `{"description": "...", "before": ..., "after": ...}`.
- `watchdog`: `{"event": "connection_lost", "details": {...}}`.
- `error`: `{"context": "...", "error_type": "...", "message": "...",
  "traceback": "...", "state_summary": {...}}` — always includes the full
  traceback and a snapshot of `AppState.summary()` at the moment of failure.

### Correlation

When a user action occurs (UI button, MIDI button, API call),
`DiagnosticsLogger.log_user_action()` generates a `correlation_id` and logs
the action under it. That id must then be threaded through to every
OSC/MIDI message and state change the action triggers (pass
`correlation_id=...` down the call chain), so the full cause-and-effect
chain of one action — click → OSC writes → console replies → state change
— can be filtered out of the log by that one id.

### Debug bundle

`app.diagnostics.export.build_debug_bundle()` (wired to `POST
/api/diagnostics/export` and the "Export Debug Bundle" button) produces a
zip containing `events.jsonl` (full on-disk history), `manifest.json`,
`routing_state.json` (current + snapshot routing state), `config.json`,
`connection_info.json` (`/xinfo`), `versions.json` (Python + library
versions), and `summary.txt` (human-readable last 50 events). Design intent:
this zip alone, pasted into a Claude Code session, should be enough to
diagnose a problem with zero access to the running system.

### Rule for future modules

Every new module — MIDI service, audio engine, ML classifier, watchdog,
web routes — logs through the shared `DiagnosticsLogger` instance passed
into its constructor. Do not add a second logging mechanism and do not
`print()`.
