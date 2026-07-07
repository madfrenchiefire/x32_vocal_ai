"""Per-channel biquad notch filter bank.

Per CLAUDE.md's core design principle #1: this (plus, if enabled, the
adaptive echo canceller in app.audio.echo_cancellation) is the only thing
allowed in the real-time audio callback path. scipy.signal SOS cascade
with persistent state (sosfilt's zi), no allocation in process() itself.
The analysis thread (app.audio.detection) instructs this bank
asynchronously via add_notch/remove_notch; it never runs FFT or ML itself.

Each notch is a parametric peaking-EQ biquad with *negative* gain (the RBJ
Audio EQ Cookbook formula: https://www.w3.org/andrew/2011/audio-eq-cookbook
-- the standard, well-established design used by essentially every
parametric EQ), not scipy.signal.iirnotch's infinite-depth null. This is
deliberate: a true infinite notch has more audible phase/ringing side
effects than a limited-depth cut, and CLAUDE.md's "notch depth (-6 to
-18 dB)" setting requires an explicit, controllable depth in the first
place -- iirnotch has no gain parameter to give one.
"""
from __future__ import annotations

import time

import numpy as np
from scipy import signal


class NotchBankFullError(Exception):
    pass


def _design_peaking_biquad(frequency_hz: float, q: float, gain_db: float, sample_rate: int) -> np.ndarray:
    """RBJ peaking-EQ biquad, returned as one SOS row [b0,b1,b2,a0,a1,a2]
    (normalized so a0 == 1)."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * frequency_hz / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * a
    b1 = -2 * cos_w0
    b2 = 1 - alpha * a
    a0 = 1 + alpha / a
    a1 = -2 * cos_w0
    a2 = 1 - alpha / a

    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])


class NotchFilterBank:
    def __init__(self, sample_rate: int, max_notches: int, depth_db: float, q: float = 10.0) -> None:
        self.sample_rate = sample_rate
        self.max_notches = max_notches
        self.default_depth_db = depth_db
        self.default_q = q
        self._notches: dict[int, dict] = {}
        self._next_id = 1
        self._sos = np.zeros((0, 6))
        self._zi = np.zeros((0, 2))

    def process(self, block: np.ndarray) -> np.ndarray:
        """Real-time path: apply all active notches to one audio block (1-D
        array). No allocation beyond what sosfilt itself needs for its
        output array -- coefficients/state are only ever touched here as
        already-built arrays, never rebuilt inline."""
        if self._sos.shape[0] == 0:
            return block
        output, self._zi = signal.sosfilt(self._sos, block, zi=self._zi)
        return output.astype(block.dtype, copy=False)

    def add_notch(self, frequency_hz: float, q: float | None = None, depth_db: float | None = None) -> int:
        """Called from the analysis thread only. Returns a notch id."""
        if len(self._notches) >= self.max_notches:
            raise NotchBankFullError(f"max_notches ({self.max_notches}) already active")
        notch_id = self._next_id
        self._next_id += 1
        self._notches[notch_id] = {
            "frequency_hz": frequency_hz,
            "q": q if q is not None else self.default_q,
            "depth_db": depth_db if depth_db is not None else self.default_depth_db,
            "last_reconfirmed_monotonic": time.monotonic(),
        }
        self._rebuild()
        return notch_id

    def remove_notch(self, notch_id: int) -> None:
        self._notches.pop(notch_id, None)
        self._rebuild()

    def active_notches(self) -> list[dict]:
        return [
            {"id": notch_id, "frequency_hz": p["frequency_hz"], "q": p["q"], "depth_db": p["depth_db"]}
            for notch_id, p in self._notches.items()
        ]

    def find_notch_near(self, frequency_hz: float, tolerance_hz: float) -> int | None:
        """Id of an active notch within tolerance_hz of frequency_hz, or
        None. Used by the analysis thread to recognize "this candidate is
        the same tone an existing notch is already suppressing" rather
        than placing a redundant second notch right next to it."""
        for notch_id, params in self._notches.items():
            if abs(params["frequency_hz"] - frequency_hz) <= tolerance_hz:
                return notch_id
        return None

    def touch_notch(self, notch_id: int, now: float | None = None) -> None:
        """Marks a notch as reconfirmed -- its tone is still showing up as
        active feedback, so release_stale_notches() must not age it out."""
        if notch_id in self._notches:
            self._notches[notch_id]["last_reconfirmed_monotonic"] = now if now is not None else time.monotonic()

    def release_stale_notches(self, max_age_sec: float, now: float | None = None) -> list[int]:
        """Removes (and returns the ids of) notches not reconfirmed in over
        max_age_sec -- CLAUDE.md's "live mode: slow release of unused
        notches." Callers gate this by mode (ring-out mode never calls
        this, "locking" its filters for the session)."""
        now = now if now is not None else time.monotonic()
        stale = [
            notch_id
            for notch_id, params in self._notches.items()
            if now - params["last_reconfirmed_monotonic"] > max_age_sec
        ]
        for notch_id in stale:
            self.remove_notch(notch_id)
        return stale

    def _rebuild(self) -> None:
        """Rebuilds the SOS cascade from the current notch set. Only called
        from add_notch/remove_notch (analysis thread) -- process() itself
        never calls this, so the allocation here never happens on the
        real-time path. Filter state resets on rebuild (a notch add/remove
        is rare relative to the audio block rate; the resulting
        discontinuity is a single-block transient, not audible drift)."""
        if not self._notches:
            self._sos = np.zeros((0, 6))
            self._zi = np.zeros((0, 2))
            return
        self._sos = np.array([
            _design_peaking_biquad(p["frequency_hz"], p["q"], -abs(p["depth_db"]), self.sample_rate)
            for p in self._notches.values()
        ])
        self._zi = np.zeros((self._sos.shape[0], 2))
