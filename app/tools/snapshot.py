"""CLI: connect to a real X32 console and capture a routing snapshot.

Usage:
    python -m app.tools.snapshot --console 192.168.1.10

Produces a named routing snapshot JSON (app.osc.routing_snapshot) and, by
default, a full diagnostics debug bundle zip (app.diagnostics.export) --
per this session's task, "get a snapshot JSON plus a debug bundle" without
needing to reproduce the failure on a live system.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import load_config
from app.diagnostics.export import build_debug_bundle
from app.diagnostics.logger import DiagnosticsLogger
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError
from app.osc.routing_snapshot import read_routing_snapshot, save_snapshot
from app.state import AppState


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.snapshot",
        description="Connect to an X32 console and capture a routing snapshot + debug bundle.",
    )
    parser.add_argument("--console", required=True, help="Console IP address, e.g. 192.168.1.10")
    parser.add_argument("--port", type=int, default=None, help="Console OSC UDP port (default 10023)")
    parser.add_argument("--name", default=None, help="Snapshot name (default: timestamped)")
    parser.add_argument("--config", default=None, help="Path to config.json (default: ./config.json if present)")
    parser.add_argument("--out-dir", default=None, help="Override snapshot output directory")
    parser.add_argument("--log-dir", default=None, help="Override diagnostics log directory")
    parser.add_argument("--timeout", type=float, default=None, help="OSC query timeout in seconds")
    parser.add_argument("--no-bundle", action="store_true", help="Skip building the debug bundle zip")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    config = load_config(args.config)
    config.console_ip = args.console
    if args.port is not None:
        config.console_port = args.port
    if args.out_dir is not None:
        config.snapshot_dir = args.out_dir
    if args.log_dir is not None:
        config.log_dir = args.log_dir
    if args.timeout is not None:
        config.osc_timeout_sec = args.timeout

    state = AppState()
    diagnostics = DiagnosticsLogger(
        log_dir=config.resolved_log_dir(),
        ring_buffer_size=config.ring_buffer_size,
        state_provider=state.summary,
    )
    correlation_id = diagnostics.log_user_action(
        "cli_snapshot", {"console": config.console_ip, "port": config.console_port}
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

        print("Reading routing snapshot ...")
        snapshot = read_routing_snapshot(osc, diagnostics, name=args.name, correlation_id=correlation_id)
        snapshot_path = save_snapshot(snapshot, config.resolved_snapshot_dir())
        state.set_snapshot(snapshot, str(snapshot_path))
        print(f"Snapshot saved: {snapshot_path}")

        decoded_in = snapshot.decode_userrout_in()
        assigned_in = [(ch, tok) for ch, tok in enumerate(decoded_in, start=1) if tok != "OFF"]
        if assigned_in:
            print("User In channels currently assigned:")
            for ch, tok in assigned_in:
                print(f"  channel {ch}: {tok}")

        missing = (
            sum(1 for v in snapshot.userrout_in if v is None)
            + sum(1 for v in snapshot.userrout_out if v is None)
            + sum(1 for vals in snapshot.routing.values() for v in vals if v is None)
        )
        if missing:
            print(
                f"NOTE: {missing} address(es) did not reply (neither individually nor via "
                "bulk fallback) -- see the debug bundle for which ones."
            )

    except FirmwareTooOldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_snapshot firmware check failed", correlation_id=correlation_id)
        exit_code = 2
    except OscConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_snapshot connection failed", correlation_id=correlation_id)
        exit_code = 1
    except Exception as exc:  # unexpected -- still want it in the bundle
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="cli_snapshot unexpected failure", correlation_id=correlation_id)
        exit_code = 1
    finally:
        if not args.no_bundle:
            bundle_name = f"debug_bundle_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
            bundle_path = config.resolved_log_dir() / "bundles" / bundle_name
            connection_info = dict(osc.xinfo) if osc.xinfo else {}
            build_debug_bundle(bundle_path, diagnostics, state, config, connection_info=connection_info)
            print(f"Debug bundle saved: {bundle_path}")

        osc.close()
        diagnostics.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
