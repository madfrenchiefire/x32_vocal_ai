"""CLI: list available audio devices and MIDI ports.

Usage:
    python -m app.tools.list_devices [--json]

Run this to see what your PC's sound cards and MIDI ports are actually
named, then set audio_input_device / audio_output_device / midi_input_port
/ midi_output_port in config.json to the names you want (see CLAUDE.md's
device-selection requirement). Read-only: no stream or port is opened.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from app.audio.devices import list_input_devices, list_output_devices
from app.config import load_config
from app.diagnostics.logger import DiagnosticsLogger
from app.midi.devices import list_midi_input_ports, list_midi_output_ports


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.list_devices",
        description="List available audio devices and MIDI ports.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a table")
    parser.add_argument("--config", default=None, help="Path to config.json (default: ./config.json if present)")
    parser.add_argument("--log-dir", default=None, help="Override diagnostics log directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    config = load_config(args.config)
    if args.log_dir is not None:
        config.log_dir = args.log_dir

    diagnostics = DiagnosticsLogger(log_dir=config.resolved_log_dir(), ring_buffer_size=config.ring_buffer_size)
    diagnostics.log_user_action("cli_list_devices")

    # Each subsystem is enumerated independently -- e.g. no MIDI backend
    # available shouldn't hide a perfectly good audio device list, and
    # vice versa.
    audio_inputs, audio_inputs_error = _try(list_input_devices, diagnostics)
    audio_outputs, audio_outputs_error = _try(list_output_devices, diagnostics)
    midi_inputs, midi_inputs_error = _try(list_midi_input_ports, diagnostics)
    midi_outputs, midi_outputs_error = _try(list_midi_output_ports, diagnostics)

    diagnostics.log_state_change(
        "devices_enumerated",
        after={
            "audio_inputs": [d.name for d in audio_inputs] if audio_inputs is not None else None,
            "audio_outputs": [d.name for d in audio_outputs] if audio_outputs is not None else None,
            "midi_inputs": midi_inputs,
            "midi_outputs": midi_outputs,
        },
    )
    diagnostics.close()

    any_error = any(e is not None for e in (audio_inputs_error, audio_outputs_error, midi_inputs_error, midi_outputs_error))
    audio_inputs = audio_inputs or []
    audio_outputs = audio_outputs or []
    midi_inputs = midi_inputs or []
    midi_outputs = midi_outputs or []

    if args.json:
        print(json.dumps({
            "audio_inputs": [asdict(d) for d in audio_inputs],
            "audio_inputs_error": audio_inputs_error,
            "audio_outputs": [asdict(d) for d in audio_outputs],
            "audio_outputs_error": audio_outputs_error,
            "midi_inputs": midi_inputs,
            "midi_inputs_error": midi_inputs_error,
            "midi_outputs": midi_outputs,
            "midi_outputs_error": midi_outputs_error,
        }, indent=2))
        return 1 if any_error else 0

    _print_section("Audio input devices", audio_inputs, audio_inputs_error,
                    lambda d: f"[{d.index}] {d.name}  ({d.host_api}, {d.max_input_channels} in, {d.default_sample_rate:.0f} Hz)")
    _print_section("Audio output devices", audio_outputs, audio_outputs_error,
                    lambda d: f"[{d.index}] {d.name}  ({d.host_api}, {d.max_output_channels} out, {d.default_sample_rate:.0f} Hz)")
    _print_section("MIDI input ports", midi_inputs, midi_inputs_error, str)
    _print_section("MIDI output ports", midi_outputs, midi_outputs_error, str)

    print(
        "\nSet these in config.json as audio_input_device / audio_output_device / "
        "midi_input_port / midi_output_port."
    )
    return 1 if any_error else 0


def _try(fn, diagnostics: DiagnosticsLogger) -> tuple[list | None, str | None]:
    try:
        return fn(), None
    except Exception as exc:
        diagnostics.log_error(exc, context=f"{fn.__module__}.{fn.__name__} failed")
        return None, str(exc)


def _print_section(title: str, items: list, error: str | None, render) -> None:
    print(f"\n{title}:")
    if error is not None:
        print(f"  ERROR: {error}")
        return
    if not items:
        print("  (none found)")
        return
    for item in items:
        print(f"  {render(item)}")


if __name__ == "__main__":
    sys.exit(main())
