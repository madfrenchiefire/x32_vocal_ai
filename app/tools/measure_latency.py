"""CLI: measure the app's audio round-trip latency -- no loopback cable.

Uses the console's own routing to loop the app's output digitally back to
its input (see app.audio.latency for the mechanism and what the figure
does/doesn't include), plays a click, and reports the round trip in
samples and milliseconds. Console routing is snapshotted and restored
around the measurement.

Usage (on the target PC, with the audio devices configured -- see
`python -m app.tools.list_devices` and the web UI's Device Setup):

    python -m app.tools.measure_latency --console 10.10.0.142

    # Use different card slots for the loop (defaults: play on 32,
    # record on 32 -- pick slots no mic channel is using):
    python -m app.tools.measure_latency --console 10.10.0.142 \\
        --out-slot 31 --in-slot 32
"""
from __future__ import annotations

import argparse
import sys

from app.audio.latency import LatencyMeasurementError, measure_round_trip_latency
from app.config import load_config
from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError
from app.state import AppState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.measure_latency",
        description="Measure app<->console round-trip latency via a console-internal digital loopback.",
    )
    parser.add_argument("--console", required=True, help="Console IP address")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--out-slot", type=int, default=32, help="Card channel the click plays out on (default 32)")
    parser.add_argument("--in-slot", type=int, default=32, help="Card return the click is detected on (default 32)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-dir", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    config = load_config(args.config)
    config.console_ip = args.console
    if args.port is not None:
        config.console_port = args.port
    if args.log_dir is not None:
        config.log_dir = args.log_dir

    if config.audio_input_device is None or config.audio_output_device is None:
        print(
            "ERROR: audio input/output devices are not configured -- pick them via the web UI's "
            "Device Setup (or config.json) first. `python -m app.tools.list_devices` shows what's available.",
            file=sys.stderr,
        )
        return 2

    state = AppState()
    diagnostics = DiagnosticsLogger(
        log_dir=config.resolved_log_dir(), ring_buffer_size=config.ring_buffer_size, state_provider=state.summary
    )
    correlation_id = diagnostics.log_user_action(
        "cli_measure_latency",
        {"console": config.console_ip, "out_slot": args.out_slot, "in_slot": args.in_slot},
    )

    osc = OscConnection(
        host=config.console_ip,
        port=config.console_port,
        diagnostics=diagnostics,
        state=state,
        timeout_sec=config.osc_timeout_sec,
        xremote_interval_sec=config.xremote_interval_sec,
        min_firmware=config.min_firmware,
        reconnect_backoff_sec=config.reconnect_backoff_sec,
    )

    exit_code = 0
    try:
        print(f"Connecting to {config.console_ip}:{config.console_port} ...")
        osc.connect(correlation_id=correlation_id)
        print(f"Connected: {osc.xinfo}")

        print(
            f"Routing console loopback (card out {args.out_slot} -> card return {args.in_slot}), "
            "playing click ..."
        )
        result = measure_round_trip_latency(
            osc, diagnostics, config,
            out_card_slot=args.out_slot, in_card_slot=args.in_slot,
            correlation_id=correlation_id,
        )
        print(
            f"\nRound trip: {result.round_trip_samples} samples "
            f"= {result.round_trip_ms:.2f} ms at {result.sample_rate} Hz"
        )
        print(
            "(Digital loop only -- add roughly 1 ms for the AD/DA converter passes a real "
            "mic-to-PA path goes through.)"
        )
        if result.round_trip_ms > 10.0:
            print(
                "NOTE: above CLAUDE.md's ~10 ms round-trip target -- try a smaller ASIO buffer size."
            )
    except LatencyMeasurementError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_measure_latency failed", correlation_id=correlation_id)
        exit_code = 1
    except FirmwareTooOldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_measure_latency firmware check failed", correlation_id=correlation_id)
        exit_code = 2
    except OscConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_measure_latency connection failed", correlation_id=correlation_id)
        exit_code = 1
    except Exception as exc:
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_measure_latency unexpected failure", correlation_id=correlation_id)
        exit_code = 1
    finally:
        osc.close()
        diagnostics.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
