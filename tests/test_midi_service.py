from __future__ import annotations

import pytest

from app.midi.service import MidiService
from app.midi.slots import SlotManager


def test_midi_service_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        MidiService(config=None, diagnostics=None, state=None)


def test_slot_manager_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        SlotManager()
