"""CLI: guided troubleshooting/protocol-discovery wizard against a real console.

Walks through every "Open item to verify" in CLAUDE.md that needs a human
to change something on the actual console while this app watches for the
resulting raw value -- the Main L/R echo-cancellation reference value, the
MIDI assign-set string format, and a best-effort /meters blob capture --
plus a full passive state capture (routing, assign sets, scribble strips)
that needs no human interaction at all. Everything lands in one timestamped
JSON report; the summary at the end says exactly which CLAUDE.md constants
to update with whatever got confirmed this run.

Usage:
    python -m app.tools.diagnose_console --console 192.168.1.10

    # Just the passive capture (routing/assign-set/scribble-strip state),
    # no interactive watch steps -- useful right after a crash, to bundle a
    # capture without standing at the console:
    python -m app.tools.diagnose_console --console 192.168.1.10 --passive-only

    # Also watch arbitrary addresses not covered by the guided steps (e.g.
    # a still-unconfirmed routing-block enum):
    python -m app.tools.diagnose_console --console 192.168.1.10 \\
        --watch /config/routing/CARD/9-16 /config/routing/CARD/17-24
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import load_config
from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError
from app.osc.protocol_discovery import (
    capture_full_state,
    capture_meters_sample,
    sniff_pushed_changes,
    watch_until_changed,
)
from app.state import AppState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.diagnose_console",
        description=(
            "Guided troubleshooting/protocol-discovery capture: connects to a real console, "
            "captures everything readable right now, then walks through CLAUDE.md's still-open "
            "protocol items (Main L/R echo reference value, MIDI assign-set format, /meters "
            "blob) so you can confirm them by changing something on the console while this "
            "watches for the result."
        ),
    )
    parser.add_argument("--console", required=True, help="Console IP address")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--out-dir", default=None, help="Override output directory (default: <log_dir>/protocol_discovery)"
    )
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--timeout", type=float, default=None, help="OSC query timeout in seconds")
    parser.add_argument(
        "--passive-only", action="store_true",
        help="Only run the full passive state capture; skip every interactive watch step.",
    )
    parser.add_argument(
        "--watch-timeout", type=float, default=120.0,
        help="Seconds to wait for a change during each guided watch step (default 120).",
    )
    parser.add_argument(
        "--watch", nargs="+", default=None, metavar="ADDRESS",
        help="Also watch these extra addresses for a change, in addition to the guided steps.",
    )
    parser.add_argument("--skip-meters", action="store_true", help="Skip the /meters capture attempt.")
    return parser


def _prompt_and_watch(osc, addrs, diagnostics, correlation_id, label, instructions, timeout_sec) -> dict:
    print(f"\n--- {label} ---")
    print(instructions)
    print(f"Watching for up to {timeout_sec:.0f}s ... (Ctrl+C to skip this step)")

    def _tick(elapsed: float) -> None:
        remaining = timeout_sec - elapsed
        print(f"\r  ...{remaining:4.0f}s remaining", end="", flush=True)

    try:
        changed = watch_until_changed(
            osc, addrs, diagnostics, timeout_sec=timeout_sec, on_tick=_tick, correlation_id=correlation_id,
        )
    except KeyboardInterrupt:
        print("\n  Skipped.")
        return {}
    print()
    if changed:
        print(f"  CONFIRMED -- {len(changed)} address(es) changed:")
        for addr, diff in changed.items():
            print(f"    {addr}: {diff['before']} -> {diff['after']}")
    else:
        print("  No change detected within the timeout -- nothing confirmed this run.")
    return changed


def _prompt_and_sniff(osc, diagnostics, correlation_id, label, instructions, duration_sec) -> list:
    """Record everything the console pushes (via the active /xremote
    subscription) while the human makes a change on the desk -- for
    discovering addresses we don't know in advance, where polling guessed
    addresses (watch_until_changed) can't work. Returns [(address, args)]."""
    print(f"\n--- {label} ---")
    print(instructions)
    print(f"Recording everything the console pushes for up to {duration_sec:.0f}s ... "
          "(Ctrl+C to stop early and keep what's been captured)")

    def _tick(elapsed: float) -> None:
        remaining = duration_sec - elapsed
        print(f"\r  ...{remaining:4.0f}s remaining", end="", flush=True)

    messages = sniff_pushed_changes(
        osc, diagnostics, duration_sec=duration_sec, on_tick=_tick, correlation_id=correlation_id,
    )
    print()
    if messages:
        print(f"  Captured {len(messages)} pushed message(s):")
        for addr, msg_args in messages:
            print(f"    {addr} {msg_args!r}")
    else:
        print("  Nothing was pushed -- either nothing changed on the console, or that control's"
              " changes aren't broadcast via /xremote.")
    return [{"address": addr, "args": list(msg_args)} for addr, msg_args in messages]


def _capture_meters(osc, diagnostics, correlation_id, out_dir: Path, timestamp: str) -> dict:
    print("\n--- /meters blob capture (best-effort) ---")
    print(
        "Subscribing via the documented form (/meters with the blob path as a string arg);\n"
        "the blob layout is still unconfirmed either way."
    )
    meters_report: dict = {}
    for meter_path in ("/meters/1", "/meters/2"):
        print(f"  /meters <- {meter_path!r} ...")
        messages = capture_meters_sample(
            osc, diagnostics, meter_path=meter_path, listen_sec=3.0, correlation_id=correlation_id,
        )
        entry: dict = {"message_count": len(messages)}
        if messages:
            bin_path = out_dir / f"meters_{meter_path.strip('/').replace('/', '_')}_{timestamp}.bin"
            with bin_path.open("wb") as f:
                for msg in messages:
                    for arg in msg:
                        if isinstance(arg, (bytes, bytearray)):
                            f.write(bytes(arg))
            entry["raw_dump"] = str(bin_path)
            print(f"    got {len(messages)} message(s) -- raw bytes saved to {bin_path}")
        else:
            print("    nothing came back.")
        meters_report[meter_path] = entry
    return meters_report


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    config = load_config(args.config)
    config.console_ip = args.console
    if args.port is not None:
        config.console_port = args.port
    if args.log_dir is not None:
        config.log_dir = args.log_dir
    if args.timeout is not None:
        config.osc_timeout_sec = args.timeout
    out_dir = Path(args.out_dir) if args.out_dir else config.resolved_log_dir() / "protocol_discovery"

    state = AppState()
    diagnostics = DiagnosticsLogger(
        log_dir=config.resolved_log_dir(), ring_buffer_size=config.ring_buffer_size, state_provider=state.summary,
    )
    correlation_id = diagnostics.log_user_action("cli_diagnose_console", {"console": config.console_ip})

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

    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: dict = {"created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    exit_code = 0

    try:
        print(f"Connecting to {config.console_ip}:{config.console_port} ...")
        osc.connect(correlation_id=correlation_id)
        print(f"Connected: {osc.xinfo}")

        print("\nCapturing full passive state (routing, assign sets, scribble strips) ...")
        report["full_state"] = capture_full_state(osc, diagnostics, correlation_id=correlation_id)
        print("  done.")

        if not args.passive_only:
            report["push_sniff"] = _prompt_and_sniff(
                osc, diagnostics, correlation_id,
                "Console-push address discovery",
                (
                    "General-purpose discovery: change anything on the console whose OSC address or\n"
                    "value format this project doesn't know yet, and every message the console pushes\n"
                    "is recorded verbatim -- this is how the assign-set addresses and value format\n"
                    "were found. Nothing specific pending right now; skip with Ctrl+C if not needed."
                ),
                args.watch_timeout,
            )

            if args.watch:
                report["extra_watch"] = _prompt_and_watch(
                    osc, args.watch, diagnostics, correlation_id,
                    "Extra address(es) you asked to watch",
                    "Change whatever you're testing on the console now.",
                    args.watch_timeout,
                )

            if not args.skip_meters:
                report["meters_capture"] = _capture_meters(osc, diagnostics, correlation_id, out_dir, timestamp)

        report_path = out_dir / f"report_{timestamp}.json"
        with report_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nFull report saved: {report_path}")

        print("\n=== Summary ===")
        printed_something = False
        # A real console pushes the same address repeatedly while a control
        # is being adjusted -- summarize each distinct address+value once.
        seen: set = set()
        for pushed in (report.get("push_sniff") or []):
            key = (pushed["address"], tuple(pushed["args"]))
            if key in seen:
                continue
            seen.add(key)
            print(f"Console pushed: {pushed['address']} = {pushed['args']!r}")
            printed_something = True
        if not args.passive_only and not printed_something:
            print("No new protocol values confirmed this run -- rerun and make the console change during the watch window.")

    except FirmwareTooOldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_diagnose_console firmware check failed", correlation_id=correlation_id)
        exit_code = 2
    except OscConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_diagnose_console connection failed", correlation_id=correlation_id)
        exit_code = 1
    except Exception as exc:
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_diagnose_console unexpected failure", correlation_id=correlation_id)
        exit_code = 1
    finally:
        osc.close()
        diagnostics.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
