# X32 SonicSniper — Project Spec

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
  order this app depends on. **Deployment gotcha confirmed on the real target PC
  (2026-07-14): recent `sounddevice` wheels bundle a PortAudio DLL built WITHOUT
  ASIO support** (Steinberg's ASIO SDK license stopped allowing redistribution) —
  `list_devices --all` shows no "ASIO" host API at all, even with the vendor ASIO
  driver installed and working in other apps. Remedies, easiest first: pin
  `sounddevice==0.4.4` (older wheels still bundled ASIO), or replace
  `site-packages/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll` with
  an ASIO-enabled build (pre-2022 history of github.com/spatialaudio/
  portaudio-binaries, or build via vcpkg's `portaudio[asio]`). Also confirmed:
  the target PC's card is the **X-LIVE** (32×32 USB audio via its own
  "X-LIVE ASIO Driver"), not the X-USB — same capability, and the app selects
  devices by name so either works. The X-LIVE's Windows class driver only
  exposes stereo IN 1-2/OUT 1-2 endpoints, so ASIO is genuinely mandatory for
  the 32-channel path, not just preferred: **Card slot N is assumed to be channel index N-1 of
  the opened stream**, everywhere from `app.audio.engine.AudioEngine`'s
  `filter_banks`/`echo_cancellers` dict keys to `app.osc.routing_apply`'s Card slot
  bookkeeping. A non-ASIO wrapper can remap or downmix channels, silently breaking
  that assumption. `AudioEngine.start()` additionally validates the configured
  devices actually have enough channels for what's been provisioned, raising a
  clear `AudioEngineError` instead of an opaque PortAudio failure (or worse, silently
  opening fewer channels than expected) if not.
- **Input index = console channel number; output index = Aux slot (insert-based
  routing, 2026-07-14).** Because the Card output block is set to Local 1:1, Card
  channel N carries console Local N, so the app *reads* channel N on input index
  N-1. `AudioEngine.filter_banks`/`echo_cancellers` are therefore keyed by console
  channel number (`AppState.channels`' own key), and `_analyze_block`/the callback
  gate on `state.channels[channel_number]` directly — no reverse lookup. The
  processed audio is *written* to a **different** index: the channel's Aux/PC-output
  slot K (`ChannelState.card_out_slot`, 1..6), which feeds Aux In K → the channel's
  insert return (`AudioEngine._output_index_for_channel`). Every other output is
  zeroed — the console only reads Card 1-N for the aux returns. (This replaced an
  earlier card-slot-vs-channel reverse-lookup that the userrout-swap design needed;
  the insert design assigns each channel its own input index, so that whole class
  of bug is gone.)
- Per-channel **live level meters** (implemented, `AudioEngine._rms_dbfs`/
  `_meters_loop`): RMS dBFS computed directly from each channel's raw captured
  audio in the callback (cheap dict write, no allocation), broadcast at ~150ms
  intervals via an injectable `on_levels_update` hook (`app/web/sockets.py` emits
  it as the `channel_meters` WebSocket event; `GET /api/channels/meters` is the
  REST fallback for the moment before the first broadcast lands) — keyed by console
  channel number (the input index the channel is read on). This measures the app's
  own captured audio, not the OSC `/meters` blob described below.
- `sounddevice` (PortAudio) via the ASIO driver, 48 kHz, 64–128 sample buffer.
  Target total round trip ≤ ~10 ms; measure it (loopback click test).
  **X-LIVE recommended stable setting (community/videos): 64 samples with
  the driver's "Safe Mode" ON.** Safe Mode is a vendor control-panel option
  (extra USB-streaming buffer for glitch-free playback) that ASIO does NOT
  expose to PortAudio, so the app can't toggle it — it's set in the X-LIVE
  ASIO panel and left on. The 64-sample buffer is what the app requests
  (`AppConfig.audio_block_size`, passed as `blocksize` to both the live
  engine's stream and `app.audio.latency`'s `sd.playrec` — the latter used
  to omit it and silently inherit the driver's larger default). Set
  `audio_block_size: 64` in config.json so the live engine and the latency
  tool match; expect the measured round trip to sit a few ms above 64/48k
  because Safe Mode's extra buffering is included in the honest figure.
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
  **How the reference actually reaches a Card channel — corrected via
  X32_OSC.pdf (2026-07-13):** there is NO direct "Main L/R" value in the
  `userrout/out` enum. The values 183/184 read off a real console (2026-07-07,
  firmware 4.13) mean **"Output 15"/"Output 16"** (`169 + N - 1`,
  `app.osc.addresses.output_userrout_out_value`) — a userrout/out slot taps a
  *physical output's* signal, and Outputs 15/16 carried Main L/R only because
  the console's Out 1-16 tab patched them that way (`/outputs/main/NN/src` =
  1/2 = Main L/Main R — the X32 factory default for outputs 15/16, but not
  guaranteed). `auto_route_reference_signal` therefore first **discovers**
  which outputs are patched to Main L/R (`find_main_lr_outputs`, reads all 16
  `/outputs/main/NN/src` values) and taps those; if no output is patched to
  Main L/R it raises with instructions rather than repatching a physical XLR
  output itself — those jacks may be feeding real speakers, and hijacking one
  silently is the opposite of gig-safe. The values are distinct L and R taps,
  never one value written to both Card channels (an earlier bug would have
  duplicated mono into both "reference" channels).
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
- **Routing automation — INSERT-BASED (rewritten 2026-07-14, `app/osc/routing_apply.py`).**
  The earlier approach (swap each channel's `/config/userrout/in/NN` to a Card
  return + flip its block to a User In bank) **did not work in practice and was
  removed.** Routing is now done through each channel's **insert** point over one
  of the 6 Aux buses — confirmed against the target console's own setup screens
  (`Resources/AuxIn.png`, `AuxOut.png`, `CardOutput.png`, `ChannelInsert.png`,
  `Input.png`). At most **6 channels** at once (one Aux bus each,
  `AppConfig.max_insert_channels`, banks of 2/4/6). The "Apply" button:
  1. Snapshot (extended, schema 4): the existing routing/userrout reads **plus**
     all 6 `/outputs/aux/NN/src` and all 32 channels' `/ch/NN/insert/{on,pos,sel}`.
  2. Assign each selected channel an Aux/PC-output slot K (1..6), stable across
     re-applies. Set the **Card output block** covering that channel to Local
     (`/config/routing/CARD/<block>` = AN…, `card_block_local_value`) so the PC
     can read the channel off the card 1:1.
  3. Set the **Aux-In remap** `/config/routing/IN/AUX` = Card 1-N (rtina 10/11/12
     via `aux_in_card_remap_value`) — the insert *returns* arrive back from the PC
     on Card 1-N, remapped onto Aux In 1-N.
  4. Set each used **Aux output** to Insert (`/outputs/aux/K/src` =
     `AUX_OUT_SRC_INSERT`) — **only when that raw value is known** (see "Open items
     to verify": the v4.09 doc enum has no "Insert" entry; it's a newer-firmware
     addition). If unconfirmed, the write is skipped and the apply response flags
     `aux_out_insert_unconfirmed` so the UI tells the user to set it on the desk;
     the value is never guessed. `AppConfig.aux_out_insert_src_value` overrides.
  5. Switch each channel's insert on: `/ch/N/insert/pos` = POST,
     `/ch/N/insert/sel` = AUX K (`insert_sel_aux_value`, enum 17-22), then
     `/ch/N/insert/on` = ON last.
  - Signal path per managed channel N (Aux slot K): preamp → Card out (Local 1:1)
    → **PC in N** → notch filtering → **PC out K** → Card in K → Aux In K →
    channel N insert return (POST), replacing the strip signal. The audio engine
    therefore **reads channel N on Card-input index N-1 and writes its processed
    audio to Card-output index K-1** (`ChannelState.card_out_slot` = K); filter
    banks are keyed by console channel number, not by a card slot.
  - Pace writes; read back every written address to confirm (settle-delay retry
    via `query_until_match`); raise if any mismatch.
- **Per-channel bypass/restore** = a single `/ch/N/insert/on 0|1` write (toggle),
  no read-modify-write of anything larger. Bypass drops the channel back to its
  own dry signal; re-insert closes the loop again. Requires the channel to have
  been applied this session (its Aux slot + insert config already in place).
- **Console-side safety scene** (implemented, `app/osc/scene.py`): before the
  app's first routing write of a session, `POST /api/routing/apply` saves a
  real scene into the console's own scene list (`/save ,siss scene <slot>
  <name> <note>`, doc-confirmed reply `/save scene <0|1>`), so the pre-app
  state is recallable from the desk's Scenes page even with the PC dead —
  the restore path that needs no PC. The slot is `AppConfig.safety_scene_slot`
  (**default None = off**: scene slots hold real show data, and the app must
  never overwrite one the user didn't explicitly choose — the web UI's
  "Save Safety Scene" control sets the slot with an are-you-sure prompt and
  persists it). Saved once per session, not per apply; a *failed* safety
  save blocks the apply rather than proceeding without the net.
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

  (Insert/bypass CCs finalized as 21–24 (Set A) / 25–28 (Set B) — `app/midi/slots.py`.
  On the console each set's physical controls are 4 encoders + buttons numbered
  5–12, so the table's "Btn 1–4" = console btn/5–8 and "Btn 5–8" = btn/9–12.)
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
  showing Aux slot (the channel's insert Aux bus / PC-output slot), active
  notch count, AI toggle, MIDI slot, mode, sensitivity, and a bypass/insert
  button. **Shows 4 channels by default, not
  all 32** — a "+ Add channel" dropdown brings any specific channel into view,
  a "Show all 32" checkbox is the escape hatch back to the full grid, and the
  visible set persists in the browser's `localStorage` across reloads. A
  channel that's actually in play (inserted, or still holding a Card slot from
  earlier in the session) is always shown regardless of this filter, so it can
  never silently disappear from view. Channel *names/colors* pulled live from
  the console via scribble-strip reads are now wired into this grid: each row
  shows the channel's console name plus a color swatch decoded from
  `app.osc.scribble_strip.read_all_channel_configs` (64 paced queries — the
  `/ch/NN/config/name` and `/ch/NN/config/color` *leaf* addresses; **the
  parent `/ch/NN/config` node gets no reply to a live bare query on real
  hardware** (confirmed 2026-07-07, two captures, all 32 channels — same
  parent-vs-leaf pattern as the routing tree's bulk nodes, which also only
  appear in scene dumps), which is why the first version of this feature
  showed no names against a real console) via
  `app.state.AppState.apply_channel_configs`. The color leaf's int→token
  enum (`app.osc.scribble_strip.SCRIBBLE_COLORS`, 8 colors + 8 inverted) is
  doc-confirmed (X32_OSC.pdf: `/ch/NN/config/color` enum int 0-15). This is deliberately
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
- **Commit notches to the console's own EQ** (implemented, `app/osc/channel_eq.py`
  + `POST /api/channels/<n>/eq/commit` / `/eq/restore`, "EQ→Desk" / "EQ Undo"
  buttons in the routing grid): writes up to 4 of the channel's deepest active
  app notches into the channel's console 4-band parametric EQ
  (`/ch/NN/eq/[1-4]/{type,f,g,q}` + `/eq/on`, all doc-confirmed in X32_OSC.pdf),
  so a ring-out's result persists with the PC fully out of the audio path.
  Details that matter: X32 OSC floats are **normalized 0.0-1.0** over each
  parameter's documented range (freq logf 20-20k/201 steps, gain linf ±15dB/
  0.25 steps, Q logf 10→0.3/72 steps — note Q's scale is inverted), conversions
  in `channel_eq.py`; console gain floors at −15 dB so deeper app notches are
  clamped (reported per band in the response); readback verification uses
  per-parameter step-grid tolerances since the console quantizes; the channel's
  full pre-commit EQ state is snapshotted first
  (`AppState.console_eq_snapshots`) and restorable via `/eq/restore` —
  deliberately NOT part of the crash watchdog's automatic restore, since a
  committed EQ is *meant* to outlive the app; a partially-failed commit still
  keeps the snapshot so restore stays available.
- **Internal vs External EQ** (implemented, per-channel `ChannelState.eq_mode`,
  "EQ" column Ext/Int in the routing grid, `app/osc/console_eq_sync.py`):
  - **External** (default): the app processes the channel's audio through its
    own notch bank via the insert (the whole routing design above). The
    console EQ is never touched — the safe choice for a channel whose desk EQ
    the engineer has already dialed in.
  - **Internal**: the channel is **not** inserted (`apply_routing` skips
    internal-EQ channels). The app only *listens* — it already reads every
    channel off the card for metering/analysis — and mirrors the feedback
    notches its detector finds into the channel's own console 4-band EQ. The
    same detection pipeline runs; only the *output* differs. The engine fires
    `on_internal_eq_update(channel)` when an internal channel's notch set
    changes, enqueuing a sync on `ConsoleEqSync` (a queue worker like
    `gain_assist` — never blocks the analysis thread), which writes via
    `channel_eq.write_notches_to_console_eq` (the snapshot-free half of the
    commit path). Gig-safe: the channel's console EQ is snapshotted **once**
    into `AppState.console_eq_snapshots` before the first write and restored
    when feedback clears, when the channel switches back to External, or on
    shutdown (`restore_all`). Inherent limit: the X32 channel EQ has only 4
    bands, so internal mode fits at most 4 feedback notches and shares them
    with any tonal EQ — which is exactly why External (unlimited app notches,
    console EQ untouched) is the default. The web UI confirms before enabling
    internal mode on a channel.
- Per-channel **input meters** show on **every** channel row now, not just
  inserted ones — the engine computes RMS dBFS for every Card input it reads
  (Card out = Local 1:1), so the meter is a live "is audio arriving on this
  channel" check even before a channel is applied.
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
  doesn't exist).
- **Preamp gain assist** (implemented, `app/osc/gain_assist.py` + `POST
  /api/gain_assist/toggle` / `/restore` / `GET /status`, "Gain assist"
  toggle in the Global card): OPT-IN last resort (`AppConfig.
  gain_assist_enabled`, default False — touching gain changes the
  engineer's mix). When a channel's notch bank is saturated and the
  detector still finds new candidates (`AudioEngine.on_notch_bank_saturated`
  hook, fired from the analysis thread, enqueue-only), a worker thread
  steps that channel's preamp (`/headamp/NNN/gain`, doc-confirmed) down by
  `gain_assist_step_db` (default 2 dB), per-channel cooldown, hard-capped
  at `gain_assist_max_total_db` (default 6 dB) per headamp per session.
  Every trim is a loud `watchdog` diagnostics event. Headamp resolution:
  `/-ha/<ch-1>/index` live mapping first (doc-confirmed), falling back to
  the pre-app source derived from the routing snapshot's IN-block value
  (Local 1-32 → headamp 0-31, AES50-A → 32-79, AES50-B → 80-127; Card/Aux
  sources have no preamp → no trim). First trim of each headamp snapshots
  its original gain; "Restore Gains" puts everything back. Deliberately
  NOT in the crash watchdog's automatic restore: mid-crash, a mic left a
  few dB quieter is safer than restoring gain a runaway squeal forced down.
- **Panic button** (implemented, `app/osc/panic.py` + `POST /api/panic` /
  `/api/panic/restore`, red PANIC/UN-PANIC button in the Global card):
  instantly mutes every app-managed channel (any channel holding a Card slot
  this session) via `/ch/NN/mix/on 0`. Pre-panic mute states are snapshotted
  first — un-panic restores each channel to what it *was*, so a channel the
  engineer already had muted stays muted. The mute writes are deliberately
  fired unpaced before any verification (silencing the PA is the whole
  point); a channel whose snapshot read timed out still gets muted, and is
  then left muted on restore rather than guessed at.
- **Spectrum display** (implemented — the console's own RTA, `app/osc/rta.py` +
  `POST /api/rta/start`/`/stop`, "Spectrum (console RTA)" card): streams the
  desk's 100-band RTA (`/meters/15`; blob format doc-confirmed —
  100 little-endian *signed* shorts packed as 50 int32 words, dB = short/256,
  exactly 0x0000 = clipping; decoder `app.osc.meters.decode_rta_blob`) over
  the `rta` WebSocket event into a canvas, with the selected channel's live
  notch frequencies (`GET /api/channels/<n>/notches`) overlaid as markers.
  Selecting a channel writes `/-stat/rtasource` (doc-confirmed: 0-31 =
  channels 1-32 pre-EQ) so the console's RTA follows it — snapshot-first,
  restored on stop, and left untouched entirely if the snapshot read fails
  or no channel is chosen. `RtaStreamer` re-sends the `/meters ,s
  "/meters/15"` subscribe every ~8 s (meter subscriptions die after ~10 s,
  like `/xremote`) via a persistent `OscConnection.add_address_listener`
  queue. One assumption, marked in the JS: the 100 bins are taken as
  log-spaced 20 Hz–20 kHz for marker placement — matches the desk's own RTA
  display range but unverified against a swept tone.
- **socket.io client is loaded from a CDN** (`cdn.socket.io`), not vendored
  locally — the venue PC needs internet access at least once (browser caching
  covers repeat runs offline). Verified this degrades gracefully rather than
  breaking the page if that script fails to load (e.g. no internet at a gig):
  the rest of the UI (devices, routing grid, settings, manual event-log load
  via `/api/events`) still works, just without live WebSocket push, confirmed
  with Playwright against a real running server. Worth vendoring the client
  file locally if offline-first turns out to matter more than initially
  assumed.

## Distribution: single-file .exe + offline licensing

- **Packaging** (`x32sonicsniper.spec`, `launcher.py`, `build_windows.bat`,
  `docs/PACKAGING.md`): PyInstaller `--onefile` build producing
  `dist/X32SonicSniper.exe`. `launcher.py` is the entry — it starts the web
  server (`app.main.main`) and opens the browser at the UI. The spec bundles
  the Flask template dir, `sounddevice`'s PortAudio data/DLLs
  (`collect_data_files`/`collect_dynamic_libs`), and the dynamic
  `hiddenimports` flask-socketio/mido need (`engineio.async_drivers.threading`,
  `mido.backends.rtmidi`); it excludes `onnxruntime`/`torch` (ML not built,
  training-only). **Must be built on Windows** (PyInstaller targets its host
  OS). The **ASIO PortAudio DLL** gotcha from the audio section applies to the
  bundle too — pin `sounddevice==0.4.4` or swap the DLL before building, per
  `docs/PACKAGING.md`.
- **Licensing** (`app/licensing/`, offline Ed25519-signed keys — chosen model:
  offline, machine-locked, 14-day trial):
  - `keys.py`: token = `X32SNIPER1.<b64url(payload)>.<b64url(ed25519 sig)>`;
    `sign_token`/`verify_token`, `LicenseInfo` (name/email/tier/issued/
    expires/machine), `machine_fingerprint()` (Windows `MachineGuid` +
    `uuid.getnode()` + platform, sha256) and short `machine_code()`.
    Machine-lock stores the short **machine code** (what the licensee sends
    the vendor), compared case/dash-insensitively.
  - `manager.py` `LicenseManager.status()` → states: `licensed`,
    `machine_mismatch`, `expired_license`, `trial`, `trial_expired`,
    `unlicensed`; `.functional` (licensed|trial) gates the app. `activate()`
    verifies signature + expiry + machine and persists the token;
    `store.py` keeps the token + trial state under `%LOCALAPPDATA%\X32SonicSniper`
    (clock-rollback guarded; deleting it resets the trial — the usual offline
    limit).
  - **Embedded public key only** (`public_key.py`, empty until the vendor runs
    `python -m app.tools.license_gen init`, which writes the **private** key to
    `secrets/` — gitignored, never shipped — and fills in the public half).
    `app/tools/license_gen.py` (`init`/`issue`/`verify`) is the vendor-only key
    mint, never bundled in the exe.
  - **Wiring**: `main.py` evaluates the license at startup and only starts
    OSC/MIDI/audio when `.functional`; `app.web.routes` `before_request` gate
    returns 403 for `/api/*` (except `/api/license/*`) when not functional, so
    the UI's License screen (badge top-right / auto-shown overlay when the
    trial ends) can always take a key via `POST /api/license/activate`.
  - **Honest scope**: no client-side scheme is uncrackable; signed +
    machine-locked keys deter casual sharing, not a determined cracker.

## Build phases
1. **Plumbing**: ASIO passthrough Card 1–4 → app → Card 1–4, latency measurement.
   **Latency measurement is implemented cable-free** (`app/audio/latency.py`,
   `python -m app.tools.measure_latency --console <ip>`): a User Out slot can
   tap "Card In N" (userrout/out 129-160, doc-confirmed) — the signal the app
   itself is playing out — so the console's own routing loops the app's output
   digitally back to its input; the tool snapshots/restores the two routing
   values it touches, plays a fixed-seed noise click, finds it by
   cross-correlation (with a peak-vs-mean guard so silence/noise raises
   instead of returning garbage), and reports samples + ms. The figure is the
   full USB round trip but excludes AD/DA converter passes (~1 ms combined on
   a real mic-to-PA path), since the loop never goes analog. Still needs a
   run on the target PC with the ASIO device configured to get the actual
   number; the audio I/O boundary (`run_playrec`) is injectable, which is how
   the test suite exercises everything but the hardware.
2. **OSC + MIDI control service** — done: connect, snapshot, apply/restore
   routing, per-channel bypass, Set A/B slot lifecycle, MIDI listener,
   scribble-strip feedback, and (as of the userctrl address/value
   confirmations below) live console-side Set A/B provisioning: selecting a
   channel writes its slot's sensitivity-encoder + AI-button +
   insert/bypass-button MIDI assignments to the console, deselecting
   restores them from the connect-time snapshot.
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
- **Aux-output "Insert" src value (`/outputs/aux/NN/src`) — UNCONFIRMED, the one
  gap in the insert-based routing.** The v4.09 doc enum for `/outputs/aux/NN/src`
  is `[0..76]` (OFF, Main L/R, M/C, MixBus, Matrix, DirectOut…, Monitor, Talkback)
  and has **no "Insert" entry**, but the console's OUT/AUX patch screen
  (`Resources/AuxOut.png`) clearly offers "Insert" as the first output-signal
  category — a newer-firmware addition the doc predates. `addresses.AUX_OUT_SRC_
  INSERT` / `AppConfig.aux_out_insert_src_value` are `None` until this raw integer
  is read off a real console. To confirm: read `/outputs/aux/02/src` on Jason's
  console (10.10.0.142) — AuxOut.png shows Aux Out 2 already set to Insert, so the
  reply *is* the value. Until then `apply_routing` writes everything except the
  aux-out src and flags `aux_out_insert_unconfirmed` so the UI tells the user to
  set each used Aux Out to Insert on the desk; the value is never guessed. The
  rest of the insert path is confirmed: `/ch/NN/insert/{on,pos,sel}` (doc,
  sel enum AUX1-6 = 17-22), `/config/routing/IN/AUX` Card 1-2/1-4/1-6 = rtina
  10/11/12, CARD block = Local (rtaea AN blocks 0-3).
- **`X32_OSC.pdf` (committed at the repo root) is Maillot's "Unofficial X32/M32
  OSC Remote Protocol" v4.09 — the authoritative reference this project's
  empirical findings are cross-checked against.** Everything it documents that
  this project relies on has so far matched real-hardware captures exactly
  (userrout table, userctrl string formats, /meters layouts, scribble color
  enum). When a new protocol question comes up, check the PDF first, then
  confirm on hardware where it matters.
- **`python -m app.tools.diagnose_console --console <ip>` is the one-stop tool
  for confirming protocol behavior against a real console.** It (1) captures
  every passive value the console will answer right now in one pass (routing
  snapshot, Set A/B assign-set snapshot, all 32 channels' scribble-strip
  configs -- `app.osc.protocol_discovery.capture_full_state`), then (2) offers
  two interactive discovery mechanisms: `watch_until_changed` (reads a baseline
  on *known* addresses, polls until one differs -- the `--watch ADDRESS
  [ADDRESS ...]` escape hatch), and a general-purpose sniff step
  (`sniff_pushed_changes` + `OscConnection.add_sniffer`), which records
  **every** message the console pushes via the active `/xremote` subscription
  regardless of address -- the discovery mechanism for addresses this project
  doesn't know yet (it's how the assign-set address shape and value format were
  found after polling guessed addresses provably couldn't). A sniff step's
  Ctrl+C stops early but *keeps* what was captured -- never throws away data a
  human stood at a console to produce. It also attempts a best-effort `/meters`
  capture (`capture_meters_sample` + `OscConnection.listen()`, which collects
  every reply on an address over a window instead of stopping at the first one
  like `query()`/`query_many()`), saving whatever raw bytes come back. Everything
  lands in one timestamped JSON report under `<log_dir>/protocol_discovery/`.
  `--passive-only` skips every interactive step (useful for a quick capture
  without standing at the console); each guided step is individually
  Ctrl+C-skippable.
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
  pattern). **Value format decoded (2026-07-07)** by matching that sniff
  against a screenshot of the console's own Edit Assigns screen for the same
  state: `'M'` + (`'C'` = Midi Push | `'c'` = Midi Toggle) + 2-digit 1-based
  MIDI channel + 3-digit CC number (Encoder 2 = "Ctrl Chg / Channel 02 / 1" ↔
  `'MC02001'`, Encoder 3 = "Channel 03 / 2" ↔ `'MC03002'`; Button 5 "Midi
  Push" ↔ `'MC…'` vs Button 6 "Midi Toggle" ↔ `'Mc…'`). Implemented as
  `app.osc.assign_set.midi_cc_value()`, and
  `app.midi.service._provision_slot` now actually writes each selected
  slot's three console controls (sensitivity encoder, AI button,
  insert/bypass button — buttons as Midi *Push*, since the app's CC dispatch
  acts on every 127 and a console-side Toggle would only send 127 on
  alternate presses); deselecting restores those controls from the
  connect-time snapshot. One caution kept in the code: two controls whose
  GUI showed "Channel 01" pushed a channel field of `'00'` (likely the
  console's untouched-default internal value), so writes are always
  readback-verified rather than assumed. The whole format is now also
  **doc-confirmed** (X32_OSC.pdf, User ASSIGN Section chapter), including every
  non-MIDI assignment type (`'F'` fader, `'S'` send, `'X'` effect, `'O'` mute,
  `'I'` insert, `'R'` remote, `'D'` selected-channel, `'P'` pan/page-jump) —
  the app never constructs those; snapshot/restore handles them as opaque
  values, which is all they need.
- **`/meters` blobs: structure confirmed on real hardware (2026-07-07, firmware
  4.13), slot layouts doc-confirmed (X32_OSC.pdf) and consistent with the
  captures.** Subscribing with the documented form (send the parent `/meters`
  address with the blob path as a string argument, `/meters ,s "/meters/1"` —
  a plain int subscribe sent *to* `/meters/1` gets nothing) streams one blob
  every ~50 ms for ~10 s. Each blob is `int32 count + count × float32`, both
  **little-endian** (unlike OSC's own big-endian wire format), floats 0..1.
  Layouts: `/meters/1` (96) = 32 channel input meters + 32 gate gain-reductions
  + 32 dynamics gain-reductions; `/meters/2` (49) = 16 bus + 6 matrix + 2 main
  LR + 1 mono, then the same 24 again as dynamics gain-reductions. The two real
  captures match: channel slots 0-31 jitter at the analog noise floor while
  every gain-reduction slot sits constant at unity/round-dB values with no
  signal. Decoder + layout constants: `app.osc.meters` (`decode_meter_blob`,
  `channel_meters`, `METERS1_*`/`METERS2_*` slices), validated against the
  committed captures in `logs/protocol_discovery/`. The app's own UI meters
  don't depend on this either way (they're computed from the app's captured
  audio, see "1. Audio engine").
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
  **The full table is now doc-confirmed** (X32_OSC.pdf, committed in this
  repo): `0` = OFF (previously decoded as `"UNSET(0)"` pending confirmation),
  then past Card: 161-166 Aux In 1-6, 167/168 TB Internal/External, 169-184
  **Outputs 1-16**, 185-200 P16 1-16, 201-206 Aux Out 1-6, 207/208
  Monitor L/R (userrout/out accepts 0-208; userrout/in only 0-168). This
  corrected an earlier misreading: the values `183`/`184` read off a real
  console (2026-07-07) with Main L/R patched through were labeled "Main L/R"
  but actually mean "Output 15/16" — a userrout/out slot taps a *physical
  output's* signal, and it carried Main L/R only because the Out 1-16 tab
  patched it that way (factory default). See "1b. Echo cancellation" for how
  `auto_route_reference_signal` now discovers the Main-L/R-carrying outputs
  (`/outputs/main/NN/src`, doc-confirmed enum: 0=OFF, 1=Main L, 2=Main R, …)
  instead of hardcoding 183/184.

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
