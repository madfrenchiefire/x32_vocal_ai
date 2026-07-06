"""Per-channel biquad notch filter bank -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's core design principle #1: this is the only thing allowed
in the real-time audio callback path. scipy.signal SOS sections with
persistent state, preallocated buffers, no allocation inside process().
The analysis thread (app.audio.detection) instructs this bank
asynchronously; it never runs ML or FFT itself.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy


class NotchFilterBank:
    def __init__(self, sample_rate: int, max_notches: int, depth_db: float) -> None:
        raise NotImplementedError("notch filter bank is implemented in a later phase")

    def process(self, block: numpy.ndarray) -> numpy.ndarray:
        """Real-time path: apply all active notches to one audio block.
        Must not allocate, block, or call into Python-level ML/analysis
        code."""
        raise NotImplementedError

    def add_notch(self, frequency_hz: float, q: float, depth_db: float) -> int:
        """Called from the analysis thread only. Returns a notch id."""
        raise NotImplementedError

    def remove_notch(self, notch_id: int) -> None:
        raise NotImplementedError

    def active_notches(self) -> list[dict]:
        raise NotImplementedError
