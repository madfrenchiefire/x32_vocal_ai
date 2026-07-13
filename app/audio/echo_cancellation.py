"""Adaptive echo cancellation (acoustic echo, not feedback).

Per CLAUDE.md's "1b. Echo cancellation" section: a mic picking up a
delayed, decayed copy of the PA signal (e.g. off a back wall in a large
room) needs a filter that predicts and subtracts that correlated copy --
different from the notch filter bank's job (a mic hearing its own
reinforced output build into a runaway tone).

Two pieces live here, per CLAUDE.md's explicit file-path expectation:

1. :class:`EchoCanceller` -- the real-time-safe NLMS adaptive FIR filter,
   one instance per channel with echo cancellation enabled. Runs inline in
   the audio callback (like the notch filters), not deferred to the
   analysis thread -- CLAUDE.md is explicit that NLMS coefficient
   adaptation is cheap enough for that.
2. :func:`auto_route_reference_signal` -- routes the console's Main L/R
   bus into two otherwise-unused Card channels via the same
   userrout/out OSC write path as app.osc.routing_apply, so the audio
   engine can read those two Card channels back as the reference signal.
"""
from __future__ import annotations

import time

import numpy as np

from app.config import AppConfig
from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import OscConnection
from app.state import AppState

# How the reference reaches a Card channel -- corrected understanding
# (2026-07-13, X32_OSC.pdf): there is NO direct "Main L/R" value in the
# userrout/out enum. The values 183/184 read off a real console
# (2026-07-07) actually mean "Output 15"/"Output 16" (169 + N - 1, see
# app.osc.addresses.output_userrout_out_value) -- a userrout/out slot taps
# a *physical output's* signal, and Outputs 15/16 carried Main L/R only
# because the console's Out 1-16 tab patched them that way
# (/outputs/main/NN/src = 1 (Main L) / 2 (Main R); that patch is the X32
# factory default for outputs 15/16, but not guaranteed). So
# auto_route_reference_signal below must first *discover* which outputs
# are patched to Main L/R and tap those, rather than blindly writing
# 183/184 and silently capturing whatever signal outputs 15/16 happen to
# carry on this particular console.


class EchoCancellationError(Exception):
    pass


class EchoCanceller:
    """NLMS adaptive FIR filter. One instance per mic channel with echo
    cancellation enabled; all such instances share the same reference
    signal (the console's Main L/R bus, read back via two Card channels)
    but adapt independent coefficients, since each mic's acoustic path to
    the PA (and therefore its echo) differs.

    Real-time-safety notes:
    - The reference history is a "mirrored" circular buffer (2x the
      filter length, each sample written to both halves) so a contiguous
      length-N read is always available without shifting or copying --
      the dot-product read is the hot path and must never allocate.
    - The per-sample weight update reuses a preallocated scratch buffer
      instead of letting `residual * history` allocate a fresh temporary
      every sample.
    """

    def __init__(
        self,
        filter_length_taps: int,
        step_size: float = 0.5,
        regularization: float = 1e-6,
        double_talk_energy_ratio: float = 2.0,
        energy_smoothing: float = 0.1,
    ) -> None:
        self.filter_length_taps = filter_length_taps
        self.step_size = step_size
        self.regularization = regularization
        self.double_talk_energy_ratio = double_talk_energy_ratio
        self.energy_smoothing = energy_smoothing

        self._weights = np.zeros(filter_length_taps, dtype=np.float64)
        self._ref_buffer = np.zeros(2 * filter_length_taps, dtype=np.float64)
        self._update_scratch = np.zeros(filter_length_taps, dtype=np.float64)
        self._pos = 0  # next write slot, in [0, filter_length_taps)

        self._mic_energy_ema = 0.0
        self._ref_energy_ema = 0.0

    def process(self, mic_block: np.ndarray, reference_block: np.ndarray) -> np.ndarray:
        """Predicts the echo component of mic_block from reference_block's
        history and subtracts it, returning the residual (what the notch
        filter bank should see next). Adapts continuously unless
        double-talk is detected for a given sample (mic energy
        significantly exceeds what the reference signal could plausibly
        have produced via an attenuating acoustic echo path -- a simple
        energy-ratio heuristic, not ML, per CLAUDE.md)."""
        if len(mic_block) != len(reference_block):
            raise ValueError("mic_block and reference_block must be the same length")

        n = self.filter_length_taps
        alpha = self.energy_smoothing
        output = np.empty(len(mic_block), dtype=np.float64)

        for i in range(len(mic_block)):
            sample = reference_block[i]
            self._ref_buffer[self._pos] = sample
            self._ref_buffer[self._pos + n] = sample
            self._pos = (self._pos + 1) % n
            history = self._ref_buffer[self._pos:self._pos + n]  # oldest -> newest, no copy

            predicted_echo = float(np.dot(self._weights, history))
            residual = float(mic_block[i]) - predicted_echo
            output[i] = residual

            self._mic_energy_ema += alpha * (mic_block[i] ** 2 - self._mic_energy_ema)
            self._ref_energy_ema += alpha * (sample ** 2 - self._ref_energy_ema)

            if self._mic_energy_ema <= self.double_talk_energy_ratio * self._ref_energy_ema + self.regularization:
                energy = float(np.dot(history, history)) + self.regularization
                gain = self.step_size * residual / energy
                np.multiply(history, gain, out=self._update_scratch)
                self._weights += self._update_scratch

        return output.astype(mic_block.dtype, copy=False)


def find_main_lr_outputs(
    osc: OscConnection,
    correlation_id: str | None = None,
) -> tuple[int, int]:
    """Which physical outputs (1-16) are currently patched to Main L and
    Main R on the console's Out 1-16 tab -- reads all 16
    /outputs/main/NN/src values and returns the first output sourcing
    Main L and the first sourcing Main R (the X32 factory default is
    15/16). Raises EchoCancellationError if either is missing: the app
    deliberately does NOT repatch a physical output itself -- those XLR
    jacks may be feeding real speakers, so hijacking one silently is the
    opposite of gig-safe. The error tells the user exactly what to patch
    instead."""
    src_addrs = [addresses.output_src_addr(n) for n in range(1, addresses.NUM_MAIN_OUTPUTS + 1)]
    results = osc.query_many(src_addrs, correlation_id=correlation_id)

    main_l_output = main_r_output = None
    for n in range(1, addresses.NUM_MAIN_OUTPUTS + 1):
        reply = results[addresses.output_src_addr(n)]
        if reply is None:
            continue
        if reply[0] == addresses.OUTPUT_SRC_MAIN_L and main_l_output is None:
            main_l_output = n
        elif reply[0] == addresses.OUTPUT_SRC_MAIN_R and main_r_output is None:
            main_r_output = n

    if main_l_output is None or main_r_output is None:
        raise EchoCancellationError(
            "no physical output is patched to "
            + ("Main L and Main R" if main_l_output is None and main_r_output is None
               else ("Main L" if main_l_output is None else "Main R"))
            + " -- on the console: Routing > Out 1-16, set an output pair to Main L / Main R "
            "(post fader). The echo reference taps a physical output's signal, and the app "
            "won't repatch an XLR output that may be feeding real speakers."
        )
    return (main_l_output, main_r_output)


def _free_card_slots(state: AppState, count: int) -> list[int]:
    used = {c.card_out_slot for c in state.channels.values() if c.card_out_slot is not None}
    free = [slot for slot in range(1, addresses.NUM_USERROUT_OUT + 1) if slot not in used]
    if len(free) < count:
        raise EchoCancellationError(
            f"only {len(free)} free Card channel(s) available, need {count} for the echo reference signal"
        )
    return free[:count]


def auto_route_reference_signal(
    osc: OscConnection,
    diagnostics: DiagnosticsLogger,
    config: AppConfig,
    state: AppState,
    correlation_id: str | None = None,
    pace_sec: float = 0.02,
    card_channels: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Routes the console's Main L/R signal into two Card channels via
    userrout/out, so the audio engine can read those two Card channels
    back as the echo reference. First discovers which physical outputs the
    console has patched to Main L/R (find_main_lr_outputs -- raises with a
    patch-it-yourself message if none are), then points the two chosen
    Card userrout/out slots at those outputs' tap values.

    card_channels lets the caller pick explicitly which two Card slots
    carry the reference (the web UI's Console Setup Left/Right port
    fields) -- raises EchoCancellationError if either is already claimed
    by a provisioned mic channel's card_out_slot. None (the default) keeps
    the original auto behavior: reuse config.echo_reference_card_channels
    if this session already assigned one, otherwise auto-pick whichever
    Card slots are free."""
    correlation_id = correlation_id or diagnostics.new_correlation_id()

    if card_channels is not None:
        slot_a, slot_b = card_channels
        used = {c.card_out_slot for c in state.channels.values() if c.card_out_slot is not None}
        conflicts = sorted({slot_a, slot_b} & used)
        if conflicts:
            raise EchoCancellationError(
                f"Card slot(s) {conflicts} already claimed by a provisioned mic channel"
            )
        config.echo_reference_card_channels = (slot_a, slot_b)
    elif config.echo_reference_card_channels is not None:
        slot_a, slot_b = config.echo_reference_card_channels
    else:
        slot_a, slot_b = _free_card_slots(state, 2)
        config.echo_reference_card_channels = (slot_a, slot_b)

    main_l_output, main_r_output = find_main_lr_outputs(osc, correlation_id=correlation_id)
    left_value = addresses.output_userrout_out_value(main_l_output)
    right_value = addresses.output_userrout_out_value(main_r_output)

    # slot_a taps the Main-L-carrying output, slot_b the Main-R one --
    # genuinely different values, so the two Card channels carry distinct
    # left/right signal rather than duplicate mono.
    channel_values = ((slot_a, left_value), (slot_b, right_value))

    for slot, value in channel_values:
        osc.send(addresses.userrout_out_addr(slot), value, correlation_id=correlation_id)
        time.sleep(pace_sec)

    mismatches: list[str] = []
    for slot, value in channel_values:
        addr = addresses.userrout_out_addr(slot)
        actual = osc.query_until_match(addr, value, correlation_id=correlation_id)
        if actual != value:
            mismatches.append(f"{addr}: expected {value}, got {actual}")

    if mismatches:
        error = EchoCancellationError(
            f"auto_route_reference_signal: {len(mismatches)} address(es) failed to confirm: {mismatches}"
        )
        diagnostics.log_error(error, context="auto_route_reference_signal", correlation_id=correlation_id)
        raise error

    diagnostics.log_state_change(
        "echo_reference_routed",
        after={
            "card_channels": [slot_a, slot_b],
            "main_lr_outputs": [main_l_output, main_r_output],
            "userrout_out_values": {"left": left_value, "right": right_value},
        },
        correlation_id=correlation_id,
    )
    return (slot_a, slot_b)
