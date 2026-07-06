from __future__ import annotations

import pytest

from app.audio.detection import FeedbackDetector
from app.audio.engine import AudioEngine
from app.audio.filters import NotchFilterBank
from app.audio.ml.classifier import FeedbackClassifier


def test_audio_engine_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        AudioEngine(config=None, diagnostics=None)


def test_notch_filter_bank_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        NotchFilterBank(sample_rate=48000, max_notches=12, depth_db=-12.0)


def test_feedback_detector_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        FeedbackDetector(sample_rate=48000)


def test_feedback_classifier_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        FeedbackClassifier(onnx_model_path="model.onnx")
