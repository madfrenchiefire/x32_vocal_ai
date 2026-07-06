"""CLI: write-and-verify test for an arbitrary routing address.

More general than test_write_channel.py (which is scoped to a channel's own
userrout/in value and its containing IN block) -- this writes any single
/config/routing/* address directly. Built to test whether the four-value
"User" bank pattern confirmed on rtgin (see CLAUDE.md's "Open items to
verify") also applies to rtaea/rtina/rout1/rout5 (AES50-A, AES50-B, Card,
Out, Aux blocks), one candidate value at a time, without writing new
channel-specific code for each block type. Not the apply_routing/restore
feature (still unimplemented, see app.osc.routing_apply).

Usage:
    # Try candidate value 26 ("User", first guess by pattern) on the Card
    # 9-16 block
    python -m app.tools.test_write_routing --console 10.10.0.142 \
        --address /config/routing/CARD/9-16 --value 26

Then check the console's routing matrix screen (the tab matching the
address -- e.g. the "Card" tab for a /config/routing/CARD/* address) to
see which column actually lit up. decode_routing_value() can only report
the generic "USER" placeholder until a specific sub-bank is confirmed and
added to app.osc.addresses.ROUTING_ENUM_TABLES, the same way rtgin's four
banks were confirmed one at a time.
"""
from __future__ import annotations

import argparse
import sys

from app.config import load_config
from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.test_write_routing",
        description="Manual test: write one routing address's raw value and verify it reads back correctly.",
    )
    parser.add_argument("--console", required=True, help="Console IP address")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--address", required=True, help="Any OSC address, e.g. /config/routing/CARD/9-16")
    parser.add_argument("--value", type=int, required=True, help="Raw value to write")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--timeout", type=float, default=None)
    return parser


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

    table = addresses.ROUTING_ADDRESS_TABLE.get(args.address)

    def decode(value: int) -> str:
        if table is None:
            return "(no decode table known for this address)"
        return addresses.decode_routing_value(table, value)

    diagnostics = DiagnosticsLogger(log_dir=config.resolved_log_dir(), ring_buffer_size=config.ring_buffer_size)
    correlation_id = diagnostics.log_user_action(
        "cli_test_write_routing", {"address": args.address, "value": args.value, "console": config.console_ip}
    )

    osc = OscConnection(
        host=config.console_ip,
        port=config.console_port,
        diagnostics=diagnostics,
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

        if table is None:
            print(
                f"NOTE: no known decode table for {args.address} -- values will print raw only. "
                "See app.osc.addresses.ROUTING_ADDRESS_TABLE for known addresses."
            )

        (original_value,) = osc.query(args.address, correlation_id=correlation_id)
        print(f"Before: {args.address} = {original_value} ({decode(original_value)})")
        diagnostics.log_state_change(
            "test_write_routing_before", after={"value": original_value}, correlation_id=correlation_id
        )

        print(f"Writing {args.address} = {args.value} ...")
        osc.send(args.address, args.value, correlation_id=correlation_id)

        new_value = osc.query_until_match(args.address, args.value, correlation_id=correlation_id)
        print(f"After:  {args.address} = {new_value} ({decode(new_value)})")
        diagnostics.log_state_change(
            "test_write_routing_after", after={"value": new_value}, correlation_id=correlation_id
        )

        if new_value != args.value:
            print("MISMATCH: value did not read back as written.", file=sys.stderr)
            exit_code = 1
        else:
            print(
                "\nMatch confirmed by readback. Check the console's routing matrix screen now "
                "to see which column/label actually lit up."
            )
        print(f"\nTo revert, rerun with: --address {args.address} --value {original_value}")

    except FirmwareTooOldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="test_write_routing firmware check failed", correlation_id=correlation_id)
        exit_code = 2
    except OscConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="test_write_routing connection failed", correlation_id=correlation_id)
        exit_code = 1
    except Exception as exc:
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="test_write_routing unexpected failure", correlation_id=correlation_id)
        exit_code = 1
    finally:
        osc.close()
        diagnostics.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
