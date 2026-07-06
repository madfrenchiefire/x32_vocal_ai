"""Channel-to-hardware-control slot allocation -- NOT IMPLEMENTED THIS PHASE.

Per CLAUDE.md's "Slot model": 8 channel slots (Set A = slots 1-4, Set B =
slots 5-8), each slot mapping to a fixed encoder/button CC group. Selecting
a channel grabs the lowest free slot; deselecting frees it.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_SLOTS = 8
SLOTS_PER_SET = 4


@dataclass
class SlotAssignment:
    slot: int  # 1-8
    channel: int
    set_name: str  # "A" or "B"


class SlotManager:
    def __init__(self) -> None:
        raise NotImplementedError("MIDI slot allocation is implemented in a later phase")

    def assign(self, channel: int) -> SlotAssignment:
        raise NotImplementedError

    def release(self, channel: int) -> None:
        raise NotImplementedError

    def free_slots(self) -> list[int]:
        raise NotImplementedError
