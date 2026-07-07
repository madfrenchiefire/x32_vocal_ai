"""Channel-to-hardware-control slot allocation.

Per CLAUDE.md's "Slot model": 8 channel slots (Set A = slots 1-4, Set B =
slots 5-8), each slot mapping to a fixed encoder/button CC group. Selecting
a channel grabs the lowest free slot; deselecting frees it.

CC numbers (finalized here from CLAUDE.md's table + its own suggested
insert/bypass range):
    Sensitivity (encoder): CC 11-14 (Set A) / 15-18 (Set B) -- slot + 10
    AI on/off (button):    CC 1-4  (Set A) / 5-8  (Set B) -- slot
    Insert/bypass (button):CC 21-24 (Set A) / 25-28 (Set B) -- slot + 20
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_SLOTS = 8
SLOTS_PER_SET = 4


class SlotsFullError(Exception):
    pass


@dataclass(frozen=True)
class SlotAssignment:
    slot: int  # 1-8
    channel: int
    set_name: str  # "A" or "B"
    index_in_set: int  # 1-4


def set_for_slot(slot: int) -> str:
    return "A" if slot <= SLOTS_PER_SET else "B"


def index_in_set(slot: int) -> int:
    return slot if slot <= SLOTS_PER_SET else slot - SLOTS_PER_SET


def sensitivity_cc(slot: int) -> int:
    return 10 + slot


def ai_toggle_cc(slot: int) -> int:
    return slot


def insert_bypass_cc(slot: int) -> int:
    return 20 + slot


class SlotManager:
    def __init__(self) -> None:
        self._slot_to_channel: dict[int, int] = {}
        self._channel_to_slot: dict[int, int] = {}

    def free_slots(self) -> list[int]:
        return [s for s in range(1, MAX_SLOTS + 1) if s not in self._slot_to_channel]

    def assign(self, channel: int) -> SlotAssignment:
        """Grab the lowest free slot for this channel, or return its
        existing assignment if it already has one. Raises SlotsFullError
        if all 8 slots are taken by other channels."""
        existing_slot = self._channel_to_slot.get(channel)
        if existing_slot is not None:
            return self._assignment_for(existing_slot, channel)

        free = self.free_slots()
        if not free:
            raise SlotsFullError("all 8 MIDI control slots are in use")
        slot = free[0]
        self._slot_to_channel[slot] = channel
        self._channel_to_slot[channel] = slot
        return self._assignment_for(slot, channel)

    def release(self, channel: int) -> None:
        slot = self._channel_to_slot.pop(channel, None)
        if slot is not None:
            self._slot_to_channel.pop(slot, None)

    def slot_for_channel(self, channel: int) -> int | None:
        return self._channel_to_slot.get(channel)

    def channel_for_slot(self, slot: int) -> int | None:
        return self._slot_to_channel.get(slot)

    def _assignment_for(self, slot: int, channel: int) -> SlotAssignment:
        return SlotAssignment(slot=slot, channel=channel, set_name=set_for_slot(slot), index_in_set=index_in_set(slot))
