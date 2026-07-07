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

# Raw userrout/out values for "Main L" and "Main R" as a source --
# confirmed on real hardware (firmware 4.13, 2026-07-07): reading back
# /config/userrout/out/31 and /32 after patching Main L/R (post-fader)
# through to those two Card channels showed 183 and 184 respectively --
# distinct values, not the same value for both (see
# app.osc.addresses.USERROUT_NAMED_VALUES for how this fits the rest of
# the flat userrout enumeration). The console's own GUI reaches this via a
# three-hop patch (a physical Out jack set to Main L/R, then a User Out
# bank sourced from that Out block, then a Card bank sourced from that
# User Out bank) -- but the *resulting* per-channel userrout/out value is
# still this one flat number either way, so auto_route_reference_signal
# below only ever needs the single direct write below, not that detour.
MAIN_L_USERROUT_OUT_VALUE = 183
MAIN_R_USERROUT_OUT_VALUE = 184


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
    """Routes the console's Main L/R bus into two Card channels via
    userrout/out, so the audio engine can read those two Card channels
    back as the echo reference.

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

    # slot_a carries Main L, slot_b carries Main R -- these are genuinely
    # different values (183 vs 184), not the same value written twice, so
    # the two Card channels actually carry distinct left/right signal
    # rather than duplicate mono.
    channel_values = ((slot_a, MAIN_L_USERROUT_OUT_VALUE), (slot_b, MAIN_R_USERROUT_OUT_VALUE))

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
            "userrout_out_values": {"left": MAIN_L_USERROUT_OUT_VALUE, "right": MAIN_R_USERROUT_OUT_VALUE},
        },
        correlation_id=correlation_id,
    )
    return (slot_a, slot_b)
