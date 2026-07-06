"""Feedback vs. musical-content classifier -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's "ML" section: small CNN on mel-spectrogram patches, trained
in PyTorch, exported to ONNX, inferred with onnxruntime on CPU (<1 ms
target). Confirms or vetoes candidates from app.audio.detection before a
notch is placed. Fully local -- no network calls.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy


class FeedbackClassifier:
    def __init__(self, onnx_model_path: str | Path) -> None:
        raise NotImplementedError("ML classifier is implemented in a later phase")

    def predict(self, mel_spectrogram_patch: numpy.ndarray) -> float:
        """Returns a confidence in [0, 1] that the patch is feedback (vs.
        musical content)."""
        raise NotImplementedError
