from __future__ import annotations

import pytest

from app.midi.slots import (
    SlotManager,
    SlotsFullError,
    ai_toggle_cc,
    index_in_set,
    insert_bypass_cc,
    sensitivity_cc,
    set_for_slot,
)


def test_cc_number_scheme():
    assert sensitivity_cc(1) == 11
    assert sensitivity_cc(8) == 18
    assert ai_toggle_cc(1) == 1
    assert ai_toggle_cc(8) == 8
    assert insert_bypass_cc(1) == 21
    assert insert_bypass_cc(8) == 28


def test_set_and_index_for_slot():
    assert set_for_slot(1) == "A"
    assert set_for_slot(4) == "A"
    assert set_for_slot(5) == "B"
    assert set_for_slot(8) == "B"
    assert index_in_set(1) == 1
    assert index_in_set(4) == 4
    assert index_in_set(5) == 1
    assert index_in_set(8) == 4


def test_assign_grabs_lowest_free_slot():
    manager = SlotManager()
    a = manager.assign(9)
    b = manager.assign(10)
    assert a.slot == 1
    assert b.slot == 2
    assert a.set_name == "A"


def test_assign_same_channel_twice_returns_same_slot():
    manager = SlotManager()
    first = manager.assign(9)
    second = manager.assign(9)
    assert first == second


def test_release_frees_the_slot():
    manager = SlotManager()
    manager.assign(9)
    manager.release(9)
    assert manager.free_slots() == list(range(1, 9))
    assert manager.slot_for_channel(9) is None


def test_assign_raises_when_full():
    manager = SlotManager()
    for ch in range(1, 9):
        manager.assign(ch)
    with pytest.raises(SlotsFullError):
        manager.assign(20)


def test_channel_for_slot_roundtrip():
    manager = SlotManager()
    assignment = manager.assign(17)
    assert manager.channel_for_slot(assignment.slot) == 17
