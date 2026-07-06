"""MIDI port enumeration.

Read-only discovery of available MIDI input/output ports via mido, so the
user can choose which MIDI-in and which MIDI-out port to use -- they need
not be the same port name, though on the X-USB card they typically are --
before app.midi.service (not yet implemented) is built on top of a fixed
assumption. Implemented now (unlike the rest of app.midi) because it's
side-effect-free: no port is opened, nothing is read or written.
"""
from __future__ import annotations

import mido


def list_midi_input_ports() -> list[str]:
    return mido.get_input_names()


def list_midi_output_ports() -> list[str]:
    return mido.get_output_names()
