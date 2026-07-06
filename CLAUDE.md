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
- **Device selection is user-configurable, not hardcoded.** The app must not assume
  the X-USB card is the only option, and input/output need not be the same device.
  Enumerate available PortAudio devices (`app/audio/devices.py`, implemented) and
  let the user pick which is the input device and which is the output device;
  persist the choice (`AppConfig.audio_input_device` / `audio_output_device`, by
  device name). `None` = not yet chosen — the audio engine must not silently guess.
  `python -m app.tools.list_devices` prints what's available on the current PC.
- `sounddevice` (PortAudio), expected to be the Behringer X-USB ASIO driver, 48 kHz,
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
- **Confirmed address shapes** (from Patrick-Gilles Maillot's own reverse-engineered
  parameter table and enum tables, github.com/pmaillot/X32-Behringer,
  `X32CfgMain.h` / `X32.c` — see "Open items to verify" below): each channel has
  its own individually get+set-able address —
  `/config/userrout/in/01`..`/32` and `/config/userrout/out/01`..`/48` — flagged
  `F_XET` (get+set) in that table. The six block-routing nodes are likewise
  individually addressable per 8-channel (or per-4/AUX) block: `/config/routing/IN/1-8`
  (…`/9-16`, `/17-24`, `/25-32`, `/AUX`), `/AES50A/1-8`…`/41-48` (6 blocks),
  `/AES50B/1-8`…`/41-48` (6), `/CARD/1-8`…`/25-32` (4), `/OUT/1-4`…`/13-16` (4),
  `/PLAY/1-8`…`/25-32`+`/AUX` (5). Each block's raw integer decodes to a source
  token (e.g. `AN1-8`, `CARD1-8`, `P161-8`) via the enum tables reproduced in
  `app/osc/addresses.py` (`ROUTING_ENUM_TABLES`), also lifted from Maillot's source.
  A *bulk* form of each of these also exists (`/config/userrout/in`,
  `/config/routing/CARD`, etc., carrying the whole array/group in one address) —
  confirmed to appear in `.scn` scene file dumps, but unconfirmed whether a live
  bare OSC query on the bulk address replies at all; `app/osc/routing_snapshot.py`
  queries individually first and only falls back to the bulk address per group if
  one or more individual queries in that group time out.
- **Routing automation** (the "Apply" button):
  1. Snapshot: read every individual `/config/userrout/in/NN`, `/config/userrout/out/NN`,
     and the block-routing addresses above. Store as named JSON snapshot.
  2. For each selected channel, write its `/config/userrout/in/NN` address to the
     matching Card return. Each channel's address is independent — no need to touch
     other channels' addresses in the same block.
  3. Use `/config/userrout/out/NN` + the CARD block routing to cherry-pick arbitrary
     selected channels' preamps onto Card outs.
  4. Flip block routing (`/config/routing/*/<block>`) to the User In/Out bank
     **matching that block's own channel range** (e.g. block 9-16 needs "User In
     9-16" specifically, not just any "User" value — see `user_in_block_value()`
     in "Open items to verify"), after everything is staged. Confirmed on real
     hardware: a channel's own `userrout/in/NN` value still displays correctly
     on the per-channel config screen even when its block is on the *wrong*
     User bank, but real audio for that channel would come from the other
     bank's slots instead — the block/bank match is load-bearing, not cosmetic.
  - Pace writes (a few ms between messages, UDP); read back key values to confirm
    before reporting success — but allow a short settle delay and retry before
    treating a stale immediate readback as a failed write (confirmed on real
    hardware: a block-routing write can visibly take effect on the console
    before a query sent right after the write reflects it).
- **Per-channel bypass/restore** = write that one channel's `/config/userrout/in/NN`
  (or `/out/NN`) address back to its snapshot value — a genuinely single-value
  write, no read-modify-write of a larger array needed, *provided the channel's
  block is already on the matching User bank* (true for a channel that was
  already inserted via this app; not true if the block's bank was never set or
  was set wrong). Implemented as a toggle (bypass ↔ re-insert).
- **Full restore** = replay the snapshot (web UI + crash watchdog; no physical button).
- **Console feedback**: write channel scribble-strip colors/names to show per-channel
  state (inserted vs bypassed, AI active/suppressing). Restore names/colors on disengage
  (they're in the snapshot).
- **Assign-set provisioning**: `/config/ctrl/A|B/enc|btn/N` — read/store existing Set A/B
  assignments first, then write MIDI-type assignments. Exact string encoding of MIDI
  assignments: verify against the Patrick-Gilles Maillot unofficial X32 OSC document,
  or empirically (assign one on the desk, query the parameter, copy the format).

### 3. MIDI service
- **Port selection is user-configurable, not hardcoded.** The app must not assume
  the X-USB card is the only MIDI interface, and in/out need not be the same port.
  Enumerate available MIDI ports (`app/midi/devices.py`, implemented) and let the
  user pick which is the input port and which is the output port; persist the
  choice (`AppConfig.midi_input_port` / `midi_output_port`, by port name as
  reported by `mido`). `None` = not yet chosen — the MIDI service must not
  silently guess. `python -m app.tools.list_devices` prints what's available.
- Expected to ride the X-USB card (enable "MIDI via X-USB card" in Setup → MIDI).
  Use `mido` + `python-rtmidi` on a background thread; fully isolated from the
  audio callback.
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
- **Device setup panel**: dropdowns for audio input device, audio output device,
  MIDI input port, MIDI output port (list from `app/audio/devices.py` and
  `app/midi/devices.py`), a "rescan devices" button, and a persisted selection
  (`AppConfig`). Shown before/alongside the routing panel — the rest of the app
  depends on these being chosen. Not yet implemented (Web UI beyond the
  diagnostics export endpoint is a later phase).
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
- MIDI-assignment string format for `/config/ctrl/*` — Maillot doc or empirical.
- `/meters` blob layout for the meters we need.
- Achievable ASIO buffer size / measured round-trip latency on the target PC.
- **Address shapes for userrout and block-level routing — confirmed 2026-07-06**
  from two sources: (1) a real console scene (`.scn`) file dump, and (2)
  Patrick-Gilles Maillot's own reverse-engineered parameter table and enum
  string tables in github.com/pmaillot/X32-Behringer (`X32CfgMain.h`,
  `X32.c`) — the reference implementation behind the "unofficial X32 OSC
  Protocol" doc. Both individual per-channel/per-block addresses (flagged
  `F_XET` = get+set in Maillot's table; e.g. `/config/userrout/in/01`,
  `/config/routing/CARD/1-8`) and bulk parent addresses (flagged `F_FND`;
  e.g. `/config/userrout/in`, `/config/routing/CARD`) are real nodes in the
  parameter tree. `app/osc/addresses.py` documents both; `routing_snapshot.py`
  queries individually first (confirmed get-able) and only falls back to the
  bulk address per group if an individual query times out — whether the
  bulk address itself replies live to a bare query (vs. only appearing in
  scene-file serialization) is the one part of this still unconfirmed
  against real hardware.
- **Block-routing enum values — address shapes AND enum tables cross-checked
  against two real consoles (2026-07-06)**: a live snapshot from a console
  running firmware 4.13 decoded via `ROUTING_ENUM_TABLES` reproduced the
  exact same tokens, in the same order, as the "Roxu" scene file dump
  (`AN1-8`/`AES50A OUT1-8.../P169-16`/`CARD1-8...`/etc. — see git history
  for the full comparison). Confirms both the addresses and the decode
  tables for the six routing-block enums.
- **"User In" enum values — confirmed on `rtgin`, and it's 4 values, not 1.**
  Setting a channel block's source to "User In" on the console (Setup →
  Routing) changed `/config/routing/IN/1-8` from `0` (`AN1-8`) to `20` —
  one past `rtgin`'s 20 named physical sources, matching a
  previously-unlabeled trailing `""` entry in Maillot's `XRtgin[]` array.
  Initially assumed to be one generic "User" option; **cross-checking the
  console's own Setup → Routing → Inputs matrix screen showed "User In" is
  itself split into the same four 8-channel banks as every other source
  type** (1-8/9-16/17-24/25-32), each a separate raw value: `20`/`21`/`22`/`23`
  = "User In 1-8"/"9-16"/"17-24"/"25-32", all four directly confirmed.
  `app.osc.addresses.user_in_block_value(channel)` computes the correct
  value for a given channel's own block. **This match matters, not just
  labeling**: a block set to the *wrong* User In bank (e.g. block 9-16 set
  to bank 1-8) still shows that channel's own `userrout/in/NN` value
  correctly on the per-channel config screen, but the routing matrix
  confirmed real audio for those channels would actually come from the
  *other* bank's slots — confirmed by testing exactly that mismatch on
  real hardware before correcting it.
- **There are two separate "User Routing" pools, not one — confirmed
  2026-07-06.** User In (32 slots, `/config/userrout/in/NN`, feeding INTO
  the channel strips) is distinct from User Out (48 slots,
  `/config/userrout/out/NN`, feeding a *send* — AES50 network, Card
  record, or analog output). Which pool a block's "User" enum value pulls
  from depends on the block's direction, not just its table:
  `/config/routing/CARD/9-16` set to `26` (one past `rtaea`'s 26 named
  sources) read back and displayed as **"User Out 1-8"**, not "User In
  1-8" — confirming CARD (and by the same shared table, AES50-A/AES50-B)
  pull from User *Out*, banked the same way AES50-A/B's own physical
  blocks already are (6 banks of 8, matching the 48 User Out slots).
  This matches CLAUDE.md's own routing-automation design ("use
  `userrout/out` + CARD block routing to cherry-pick arbitrary channels
  onto Card outs") rather than contradicting it. Only `rtaea`'s first bank
  (`26` = "User Out 1-8") is directly confirmed; `27`-`31` are inferred by
  the same pattern. `rtina` (IN/AUX, PLAY/AUX) and `rout1`/`rout5` (OUT,
  physical analog outputs) are still untested — by the same input-vs-output
  reasoning, `rtina` is more likely User In and `rout1`/`rout5` more likely
  User Out, but that's a guess by analogy, not confirmed. Use
  `python -m app.tools.test_write_routing --console <ip> --address
  <routing address> --value <candidate>` to test any of these without
  writing new code per block type, then check the console's routing matrix
  screen (the tab matching the address) for which column lit up.
- **`userrout/in`/`userrout/out` value semantics — confirmed for all four
  source families (2026-07-06, real hardware, firmware 4.13).** A channel
  assigned to Local Analog In 1 read back `1`; AES50-A In 2 read back `34`;
  AES50-B In 3/4 read back `83`/`84`; Card 1-8 (1:1) read back `129`-`136`.
  All four fit a single flat, 1-indexed enumeration:
  `value = range_start + (channel - 1)`, with ranges 1-32 Local Analog,
  33-80 AES50-A, 81-128 AES50-B, 129-160 Card (same source ordering as the
  block-routing tables). Implemented as
  `app.osc.addresses.decode_userrout_value()` /
  `RoutingSnapshot.decode_userrout_in()` / `.decode_userrout_out()`.
  Every untouched channel across three consoles now reads `0`; decoded as
  `"UNSET(0)"` rather than assumed to mean "off" since that specific
  meaning hasn't been separately confirmed. What lies beyond index 160
  (more AES50 sends, USB, etc.) is still unknown.

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

- `osc_tx` / `osc_rx`: `{"address": "/config/userrout/in/01", "args": [0]}`
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
