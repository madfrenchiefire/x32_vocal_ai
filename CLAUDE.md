# X32 AI Feedback Suppression — Project Spec

Real-time microphone feedback suppression for the Behringer X32, running as a locally
hosted app on a PC connected via the X-USB card (audio + MIDI) and Ethernet (OSC).
Web-based UI (Flask + WebSockets), consistent with the existing X32 Monitor Manager app.

## Core design principles

1. **The AI is never in the audio path.** The real-time audio path is biquad notch
   filtering plus (if enabled) adaptive echo cancellation — both classical DSP,
   near-zero latency. FFT analysis and ML classification run on a parallel analysis
   thread that instructs the filter bank asynchronously; the adaptive filter's own
   coefficient adaptation (NLMS) is cheap enough to run inline in the callback like
   the notch filters, not deferred to the analysis thread.
2. **Snapshot before touching anything.** Every console state the app modifies
   (routing, assign sets, scribble strips/colors) is read and stored first, and is
   restorable — per channel, globally, and automatically on crash (watchdog).
3. **Gig-safe defaults.** Any failure mode must resolve to "mics on their original
   patch, console behaving stock" within ~1 second.
4. **Set C of the assign section is off-limits.** The app only provisions Sets A and B.

## Architecture — three services, one Flask/WebSocket backend

### 1. Audio engine
- **Device selection is user-configurable, not hardcoded — but ASIO is required, not
  optional.** The app must not assume the X-USB card is the only option, and input/
  output need not be the same device. Enumerate available PortAudio devices
  (`app/audio/devices.py`, implemented) and let the user pick which is the input
  device and which is the output device; persist the choice
  (`AppConfig.audio_input_device` / `audio_output_device`, by device name). `None` =
  not yet chosen — the audio engine must not silently guess.
  `python -m app.tools.list_devices` prints what's available on the current PC.
  **`list_input_devices()`/`list_output_devices()` filter to ASIO-hosted devices only
  by default** (`asio_only=True`) — a Windows audio interface typically exposes both
  an ASIO device and one or more MME/WDM/WASAPI "wrapped" devices for the same
  physical hardware, and only the ASIO one guarantees the direct, stable channel
  order this app depends on: **Card slot N is assumed to be channel index N-1 of
  the opened stream**, everywhere from `app.audio.engine.AudioEngine`'s
  `filter_banks`/`echo_cancellers` dict keys to `app.osc.routing_apply`'s Card slot
  bookkeeping. A non-ASIO wrapper can remap or downmix channels, silently breaking
  that assumption. `AudioEngine.start()` additionally validates the configured
  devices actually have enough channels for what's been provisioned, raising a
  clear `AudioEngineError` instead of an opaque PortAudio failure (or worse, silently
  opening fewer channels than expected) if not.
- **Card slot vs console channel number — confirmed as a real bug via code audit
  (2026-07-07), fixed in `AudioEngine._channel_state_for_slot`.** `AppState.channels`
  is keyed by console channel number (1-32); `AudioEngine.filter_banks`/
  `echo_cancellers` and the audio stream's own channel indices are keyed by Card
  slot number. These only coincide when a channel's `card_out_slot` happens to equal
  its channel number. `_analyze_block`'s gating (AI enabled? which mode/sensitivity?
  echo cancellation on?) originally indexed `state.channels[card_slot]` directly —
  silently reading the wrong channel's settings (or none at all) whenever routing
  assigned a channel to a non-matching Card slot. Fixed by reverse-looking-up the
  `ChannelState` whose `card_out_slot` matches the slot actually being processed;
  a slot with no channel currently assigned to it now correctly gates closed
  instead of falling through to whatever channel number happened to match.
- Per-channel **live level meters** (implemented, `AudioEngine._rms_dbfs`/
  `_meters_loop`): RMS dBFS computed directly from each channel's raw captured
  audio in the callback (cheap dict write, no allocation), broadcast at ~150ms
  intervals via an injectable `on_levels_update` hook (`app/web/sockets.py` emits
  it as the `channel_meters` WebSocket event; `GET /api/channels/meters` is the
  REST fallback for the moment before the first broadcast lands) — keyed by Card
  slot number, same as `filter_banks`. This measures the app's own captured audio,
  not the OSC `/meters` blob described below, whose layout remains unconfirmed.
- `sounddevice` (PortAudio) via the ASIO driver, 48 kHz, 64–128 sample buffer.
  Target total round trip ≤ ~10 ms; measure it (loopback click test).
- Audio callback does per-channel biquad notch filtering plus, if echo cancellation
  is enabled for that channel, the adaptive echo canceller (`scipy`/numpy, persistent
  state, preallocated buffers, no allocation in callback).
- Analysis thread consumes a ring buffer: FFT peak detection heuristics
  (peak-to-average ratio, absence of harmonic structure, sustained growth) flag
  candidate frequencies; ML classifier confirms/vetoes (Phase 4, not yet built —
  heuristics alone place notches for now); filter bank places notches.
- ML (Phase 4, later): small CNN on mel-spectrogram patches, "feedback vs musical
  content." Train in PyTorch, export ONNX, infer with `onnxruntime` on CPU (<1 ms).
  Fully local. Needs real ring-out recordings as training data before it can be
  built for real — not something to stub out ahead of having that data.
- Training data plan (when Phase 4 starts): deliberately ring out rooms at low PA
  level across mics/positions (positives); multitrack vocals/instruments incl.
  sustained notes, whistles, cymbal swells (negatives / false-positive hard cases).

### 1b. Echo cancellation (acoustic echo, not feedback)
- **Different problem from feedback.** Feedback is a mic hearing its own reinforced
  output build into a runaway tone (handled by the notch filter bank above). Echo is
  a mic picking up a *delayed, decayed* copy of the PA signal (e.g. off a back wall in
  a large room) — the fix is an adaptive filter that predicts and subtracts that copy,
  not a notch.
- **User-configurable, off by default.** `AppConfig.echo_cancellation_enabled: bool`.
  Only meaningful once a reference signal (what's actually being sent to the PA) is
  available — without one there's nothing to correlate the mic signal against.
- **Reference signal: auto-routed from the console, not manually patched.** When
  enabled, the app uses the same OSC routing-write path as `apply_routing` to route
  the X32's Main L/R bus into two otherwise-unused Card channels
  (`AppConfig.echo_reference_card_channels: tuple[int, int] | None`, auto-picked from
  whichever Card channels aren't already claimed by a provisioned mic channel), then
  reads those two Card channels back into the app as the reference signal.
  **User-selectable, not just auto-picked**: `app.audio.echo_cancellation.
  auto_route_reference_signal(..., card_channels=(left, right))` lets the web UI's
  Console Setup "Reference L/R" fields (`POST /api/echo_cancellation/reference`)
  pick the two Card ports explicitly — validated against conflicts with any
  provisioned mic channel's `card_out_slot` — and overrides whatever was
  auto-picked or reused from a previous session.
  **Raw `userrout/out` values for "Main L" and "Main R" as a source — confirmed
  against real hardware (2026-07-07, firmware 4.13):** `MAIN_L_USERROUT_OUT_VALUE
  = 183`, `MAIN_R_USERROUT_OUT_VALUE = 184` — two distinct values, not the same
  value written to both Card channels (the code originally wrote one shared
  placeholder to both, which would have duplicated mono into both "reference"
  channels instead of true L/R; fixed alongside this confirmation). Confirmed by
  patching Main L/R (post-fader) through to two Card channels on a real console
  and reading `/config/userrout/out/NN` back via
  `python -m app.tools.diagnose_console`'s passive capture — see
  `app.osc.addresses.USERROUT_NAMED_VALUES`. The console's own GUI reaches this
  via a three-hop patch (a physical XLR output set to Main L/R → a User Out bank
  sourced from that Out block → a Card bank sourced from that User Out bank), but
  the *resulting* per-channel `userrout/out` value is still this one flat number
  either way, so `auto_route_reference_signal` only ever needs the direct
  single-address write below, not that detour.
- **Algorithm: NLMS (normalized least-mean-squares) adaptive FIR filter**, one per
  channel with echo cancellation enabled, filter length sized to the room's expected
  reflection tail (start around 200 ms at 48 kHz = ~9600 taps; tune once real rooms
  are tested — larger rooms need a longer tail). Runs in the audio callback: predict
  the echo component from the reference signal history, subtract it from the mic
  signal, then feed the *residual* into the same notch filter bank as before. Adapts
  continuously; needs basic double-talk detection (skip/slow adaptation when the mic
  signal has significant energy uncorrelated with the reference, e.g. someone talking
  over playback) so it doesn't diverge — simple energy-ratio heuristic to start, not
  ML.
- **Not a substitute for gain-before-feedback discipline** — this only removes the
  correlated echo path; it doesn't change acoustic gain structure or genuine feedback
  loops, which the notch filter bank still handles independently.

### 2. OSC service (console control, UDP 10023)
- `python-osc`. Send `/xremote` and refresh every ~8 s to receive state changes.
  Subscribe to `/meters` (binary blobs) for live channel meters.
- On connect: query `/xinfo`, require firmware 4.0+ (User In/Out routing).
- **`/xremote` gets no reply — a genuinely healthy, idle console can go quiet for
  arbitrarily long stretches.** It only tells the console "keep pushing me
  state-change notifications for the next ~10s"; if nothing on the console
  changes, nothing comes back, and that's normal, not a sign of a dead
  connection. **Confirmed as a real bug against a live console (2026-07-07)**:
  the watchdog's original "no incoming traffic in `xremote_interval_sec * 2.5`
  seconds ⇒ disconnected" heuristic mistook ordinary console silence for a lost
  connection, flapping `connected`/`disconnected` roughly every 20s whenever the
  console sat idle — including a real user-visible failure (a routing-snapshot
  request landing during one of those false "disconnected" windows got a bogus
  503). Fixed in `OscConnection._watchdog_loop`/`_probe_alive`: a stale-looking
  connection is now confirmed with one active `/xinfo` query-reply pair before
  ever being declared lost; only a failed *active* probe now triggers the
  `connection_lost` → reconnect-with-backoff path.
- **`OscConnection.query_many` starvation bug — confirmed and fixed (2026-07-07).**
  The batch-read helper used to check each address in list order, blocking on
  `queue.get(timeout=remaining)` for one address at a time. An address that never
  replies ate the *entire* remaining deadline on that one blocking call — so every
  address after it in the list got reported `None`, even ones whose reply had
  already arrived and was sitting in its own queue, because the loop gave up on the
  rest before ever checking them. Only reproduces when the unanswered address isn't
  last in the list, which is why it went unnoticed (the original test for this
  happened to put the missing address last). Fixed by polling every outstanding
  address's queue non-blockingly each sweep instead of blocking on one at a time.
  This affected any caller reading many addresses at once where a real console
  might not answer every one on the first try — e.g.
  `app.osc.scribble_strip.read_all_channel_configs` (32 addresses for the routing
  grid's name/color columns) and `app.osc.routing_snapshot`.
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
  The crash watchdog (`app.watchdog.Watchdog`) restores the routing snapshot and
  the Set A/B assign-set snapshot (`app.osc.assign_set.snapshot_assign_sets`,
  captured on connect into `AppState.assign_set_snapshot`) independently — either
  can be present without the other, so a crash before a routing snapshot exists
  still gets Set A/B restored, and vice versa.
- **Console feedback**: write channel scribble-strip colors/names to show per-channel
  state (inserted vs bypassed, AI active/suppressing). Restore names/colors on disengage
  (they're in the snapshot).
- **Assign-set provisioning**: read/store existing Set A/B assignments first, then
  write MIDI-type assignments. Address shape **confirmed by live sniff for
  encoders**: `/config/userctrl/<A|B>/enc/<1-4>`, string values (`'MC01000'` etc.);
  buttons assumed `/config/userctrl/<set>/btn/<5-12>`, and the exact digit meaning
  of the MIDI-CC value strings is still open — see "Open items to verify".

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
- **Console setup panel** (implemented, `app/web/routes.py` `/api/console/*`):
  manual host/port entry plus a "Search for Console" button
  (`app.osc.discovery.discover_consoles`, broadcasts `/xinfo` to the local
  subnet and collects whichever consoles reply within a couple of seconds —
  the same technique other X32 remote apps use to avoid requiring the user
  to already know the console's IP). Connect/disconnect are live: the
  console IP is no longer config-file-only, and connecting rewires the
  live `OscConnection` into every other route, the running `MidiService`,
  and the crash watchdog without a restart. `app/main.py` now arms the
  watchdog unconditionally at startup (even with no console configured
  yet) specifically so this endpoint has something to wire a connection
  into later. Known limitation: discovery sends to one broadcast address
  per call, so a PC with multiple NICs on different subnets needs the
  request repeated per subnet — not auto-enumerated.
- **Device setup panel** (implemented, `app/web/routes.py` `/api/devices` +
  `/api/devices/select`): dropdowns for audio input device, audio output device,
  MIDI input port, MIDI output port (list from `app/audio/devices.py` and
  `app/midi/devices.py`), a "rescan devices" button, and a persisted selection
  (`AppConfig`, written via `save_config` if the app was started with a config
  path — a bare `create_app()` call with no path updates the in-memory config
  only, so tests/ad-hoc runs never write a stray `config.json`).
- **Routing panel** (implemented, `/api/routing/*` + `/api/channels/*`):
  channel grid with a per-channel select checkbox, Save Snapshot / Apply
  Routing / Restore / Bypass All / Re-insert All buttons, per-channel rows
  showing card-out slot, active notch count, AI toggle, MIDI slot, mode,
  sensitivity, and a bypass/insert button. **Shows 4 channels by default, not
  all 32** — a "+ Add channel" dropdown brings any specific channel into view,
  a "Show all 32" checkbox is the escape hatch back to the full grid, and the
  visible set persists in the browser's `localStorage` across reloads. A
  channel that's actually in play (inserted, or still holding a Card slot from
  earlier in the session) is always shown regardless of this filter, so it can
  never silently disappear from view. Channel *names/colors* pulled live from
  the console via scribble-strip reads are now wired into this grid: each row
  shows the channel's console name plus a color swatch decoded from
  `app.osc.scribble_strip.read_all_channel_configs` (32 paced queries, one per
  channel) via `app.state.AppState.apply_channel_configs`. This is deliberately
  *not* done synchronously inside `/api/console/connect` (would add several
  seconds to that response) — the web UI calls the new `POST
  /api/channels/refresh_names` itself right after a successful connect
  (best-effort; also available as a standalone "Refresh Names" button for
  picking up console-side renames later in a session).
- **Per channel** (implemented via `/api/channels/<n>/settings`): detection
  sensitivity, max simultaneous notches (default 12), notch depth (−6 to
  −18 dB), Q/width all persist to `ChannelState` and, if a live `NotchFilterBank`
  is wired into the running `AudioEngine`, take effect immediately. "Deploy
  speed" from the original spec has no concrete field yet — not implemented.
- **Modes**: **Ring-out/setup** vs **Live** are a per-channel `ChannelState.mode`
  field, selectable in the routing grid and persisted, and now a real behavioral
  difference in `app.audio.engine._analyze_block`: ring-out mode never releases
  notches (`release_stale_notches` is only called in live mode) and detects at a
  lower, more aggressive threshold (`RING_OUT_THRESHOLD_ADJUSTMENT_DB`); live mode
  slow-releases notches unreconfirmed for `NOTCH_RELEASE_AFTER_SEC`. Per-channel
  **sensitivity** (0-1) is likewise no longer cosmetic — it's mapped to a detection
  threshold via `app.audio.detection.sensitivity_to_threshold_db` and passed into
  `FeedbackDetector.analyze` per channel.
- **Global** (implemented): bypass all / re-insert all
  (`/api/routing/bypass_all`), echo cancellation on/off + reference-channel
  status (`/api/echo_cancellation/toggle`, auto-routes via
  `app.audio.echo_cancellation.auto_route_reference_signal` the first time it's
  enabled), an explicit reference-port picker (`POST
  /api/echo_cancellation/reference`, Console Setup's "Reference L/R" fields —
  see "1b. Echo cancellation" above), live per-channel level meters (Card-slot-
  keyed dBFS bars pushed over the `channel_meters` WebSocket event, see "1. Audio
  engine" above), event log (every diagnostics event, including `notch_placed`'s
  channel/frequency/time, pushed live over WebSocket as it's logged via
  `DiagnosticsLogger.add_listener` — not polled). **Not implemented**: the ML
  confidence threshold slider (inert regardless, since Phase 5's ML classifier
  doesn't exist) and the per-channel spectrum display (blocked on the
  unconfirmed `/meters` blob layout below).
- **socket.io client is loaded from a CDN** (`cdn.socket.io`), not vendored
  locally — the venue PC needs internet access at least once (browser caching
  covers repeat runs offline). Verified this degrades gracefully rather than
  breaking the page if that script fails to load (e.g. no internet at a gig):
  the rest of the UI (devices, routing grid, settings, manual event-log load
  via `/api/events`) still works, just without live WebSocket push, confirmed
  with Playwright against a real running server. Worth vendoring the client
  file locally if offline-first turns out to matter more than initially
  assumed.

## Build phases
1. **Plumbing**: ASIO passthrough Card 1–4 → app → Card 1–4, latency measurement.
   `measure_round_trip_latency()` is implemented as a documented
   `NotImplementedError` (needs a physical loopback cable + the target PC;
   not something a test suite can exercise) — everything else in this phase
   (device enumeration/selection) is built.
2. **OSC + MIDI control service** — done: connect, snapshot, apply/restore
   routing, per-channel bypass, Set A/B slot lifecycle, MIDI listener,
   scribble-strip feedback. (Set A/B's actual console-side provisioning,
   `app/osc/assign_set.py`, is still deliberately not wired to a live write —
   see the MIDI-assignment string format open item below.)
3. **Heuristic detection + notch filter bank** — done (`app/audio/filters.py`,
   `app/audio/detection.py`).
4. **Echo cancellation** — done (`app/audio/echo_cancellation.py`): NLMS
   canceller + auto-routed reference signal (Main L/R's raw `userrout/out`
   values are confirmed, see below).
5. **ML classifier** — not started; needs real ring-out recordings first,
   deliberately not built until that data exists (`app/audio/ml/classifier.py`
   remains a documented `NotImplementedError` stub).
6. **Watchdog, event log, polish** — done: `app/watchdog.py` (signal/atexit/
   excepthook-triggered restore) and the web UI's live event log
   (`app/web/sockets.py`, pushed over WebSocket). `app/main.py` is the actual
   process entrypoint (`python -m app.main`) wiring OSC/MIDI/audio/watchdog/
   web together — every service is best-effort at startup so an unconfigured
   or unreachable one is skipped rather than fatal.

## Open items to verify (do not assume)
- **`python -m app.tools.diagnose_console --console <ip>` is the one-stop tool for
  confirming everything below against a real console.** It (1) captures every
  passive value the console will answer right now in one pass (routing snapshot,
  Set A/B assign-set snapshot, all 32 channels' scribble-strip configs --
  `app.osc.protocol_discovery.capture_full_state`), then (2) walks through each
  still-open item that needs a human to change something on the console while
  the tool watches. Two watching mechanisms, chosen per item: `watch_until_changed`
  (reads a baseline on *known* addresses, polls until one differs -- used for the
  Main L/R echo-reference cross-check and the `--watch ADDRESS [ADDRESS ...]`
  escape hatch), and `sniff_pushed_changes` (+`OscConnection.add_sniffer`), which
  records **every** message the console pushes via the active `/xremote`
  subscription regardless of address -- the discovery mechanism for addresses
  this project doesn't know yet, used for the assign-set step since polling
  guessed addresses provably can't find them (see below). A sniff step's Ctrl+C
  stops early but *keeps* what was captured -- never throws away data a human
  stood at a console to produce. It also attempts a best-effort `/meters` capture
  (`capture_meters_sample` + `OscConnection.listen()`, which collects every reply
  on an address over a window instead of stopping at the first one like
  `query()`/`query_many()`), saving whatever raw bytes come back for offline
  decoding. Everything lands in one timestamped JSON report under
  `<log_dir>/protocol_discovery/`, and the tool's final summary says exactly
  which constant to update with whatever got confirmed that run. `--passive-only`
  skips every interactive step (useful for a quick capture without standing at
  the console); each guided step is individually Ctrl+C-skippable.
- **MIDI-assignment addresses: real shape discovered by sniff (2026-07-07,
  firmware 4.13) — `/config/userctrl/<A|B>/enc/<1-4>`, string values.** The
  originally guessed `/config/ctrl/...` shape got no reply at all on a live
  console (not "empty value"; no reply, same as any unrecognized address); the
  first live run of `diagnose_console`'s sniff step then caught the console
  pushing `/config/userctrl/A/enc/1`..`/enc/3` with string values as Set A
  encoder assignments were changed on the desk: `'X000'`, `'S0000'`, `'S5000'`
  (pre-existing assignments of other types), then `'MC01000'`/`'MC03000'`/
  `'MC04000'` (MIDI-CC-type assignments). `app/osc/assign_set.py` now uses the
  confirmed shape. **Button numbering confirmed by a second sniff the same
  day**: button assignment changes pushed `/config/userctrl/A/btn/5` and
  `/btn/6` (btn/5-12; 5 and 6 observed directly, 7-12 by the now-verified
  pattern). Still open within this item: which digit group of the `'MC.....'`
  string is the CC number vs the MIDI channel — the second sniff read
  `'MC01000'`/`'MC02001'`/`'MC03002'` off encoders 1-3, where both candidate
  fields increment together, so either reading fits; assign a known,
  *asymmetric* CC + channel pair (e.g. CC 7 on channel 16) during the sniff
  step to pin it down before constructing assignment values in
  `app.midi.service._provision_slot`. Also observed: a button value
  `'Mc00000'` with lowercase 'c' — letter case apparently encodes an
  assignment sub-type (CC vs CC-toggle vs note...), un-decoded.
- **`/meters` blob *structure* confirmed on real hardware (2026-07-07, firmware
  4.13); slot *meaning* still unmapped.** Subscribing with the documented form
  (send the parent `/meters` address with the blob path as a string argument,
  `/meters ,s "/meters/1"` — a plain int subscribe sent *to* `/meters/1` gets
  nothing) streams one blob every ~50 ms for a few seconds. Each blob is
  `int32 count + count × float32`, both **little-endian** (unlike OSC's own
  big-endian wire format), floats 0..1: `/meters/1` = 96 values, `/meters/2` =
  49 values. Decoder: `app.osc.meters.decode_meter_blob` (validated against the
  committed real captures in `logs/protocol_discovery/`). Slot semantics, from
  comparing two captures taken ~19 minutes apart: **`/meters/1` slots 0-31 are
  the 32 live channel input meters** — all 32 show per-blob variance at the
  analog noise floor (~1.4e-5 ≈ -97 dBFS, different every 50ms blob, in both
  captures independently), which static parameters can't produce; **slots
  32-95 are NOT audio meters** — bit-identical constants within and across
  both captures, at round dB values (-21/-10/-20/0 dB), so whatever the
  console packs there doesn't move with audio and must not be read as levels.
  A final 1:1 index→channel check (signal on exactly one known channel) is
  still worth doing before trusting a *specific* index. The app's own UI
  meters don't depend on this either way (they're computed from the app's
  captured audio, see "1. Audio engine").
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
  the same pattern.
  `rout1` (OUT, physical analog outputs) is **also confirmed**:
  `/config/routing/OUT/1-4` set to `26` (one past `rout1`'s 26 named
  sources) displayed as "User Out 1-8" on the console's "XLR" routing
  matrix tab — notably the `OUT` block itself is 4-channels-wide but still
  pulled from an 8-wide User Out bank, confirming User Out banking is
  fixed at 8 regardless of the consuming block's own width. `rout5`
  (the other half of the same 4-wide `OUT` blocks) is assumed to share the
  pool/table by construction but hasn't been independently written to.
  `rtina` (`IN`/`AUX`, `PLAY`/`AUX`) is the only address family with zero
  data so far — by the same input-vs-output reasoning it's more likely
  User In, but that's a guess by analogy, not confirmed. Use
  `python -m app.tools.test_write_routing --console <ip> --address
  <routing address> --value <candidate>` to test any of these without
  writing new code per block type, then check the console's routing matrix
  screen (the tab matching the address) for which column lit up.
- **`rtina` fully confirmed (2026-07-06), including its own oddball
  banking.** `/config/routing/IN/AUX` set to `13` (one past `rtina`'s 13
  named sources) matched "User In 1-2" — confirmed twice over: by readback
  *and* by the console's own "Aux In Remap" dropdown, which lists the
  complete enum in order (the 13 named entries, then "User In
  1-2"/"1-4"/"1-6" at indices 13/14/15). Direction was User In as guessed,
  but the banking is 2/4/6-channel groups (matching AUX's own 6-channel
  width), not the 8-wide banks used everywhere else — a reminder that
  "banked like every other source type" doesn't hold universally, only
  where the consuming block's own width is a multiple of 8.
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
  (more AES50 sends, USB, etc.) is still unknown, except for two individual
  values confirmed below.
- **Main L/Main R `userrout/out` values confirmed (2026-07-07, real hardware,
  firmware 4.13) — 183 and 184, not one shared value.** Patched Main L/R
  (post-fader) through to two Card channels on a real console (via a
  three-hop GUI patch: a physical XLR output set to Main L/R → a User Out
  bank sourced from that Out block → a Card bank sourced from that User Out
  bank) and read `/config/userrout/out/NN` back for those two Card channels:
  `183` and `184` respectively. This also surfaced a real bug: the code's
  original `auto_route_reference_signal` wrote one single guessed placeholder
  value to *both* Card channels, which would have duplicated mono into both
  "reference" channels instead of true L/R — fixed alongside this
  confirmation (`app.audio.echo_cancellation.MAIN_L_USERROUT_OUT_VALUE` /
  `MAIN_R_USERROUT_OUT_VALUE`, `app.osc.addresses.USERROUT_NAMED_VALUES`).
  What occupies 161-182 (presumably Bus/MixBus then Matrix, by the same
  one-past-the-previous-range pattern as every other family here — see the
  comment above `USERROUT_NAMED_VALUES`) is inferred by arithmetic, not
  independently confirmed; only 183/184 themselves are.

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
