"""CLI: single-channel userrout write/verify test.

This is a manual verification tool for the OSC *write* path -- it is NOT
the apply_routing/bypass_channel/restore_snapshot feature described in
CLAUDE.md (those remain unimplemented, see app.osc.routing_apply). It
exists to confirm, against real hardware, that setting
/config/userrout/in/NN actually lands where expected.

A per-channel userrout/in value only takes visible effect once that
channel's containing 8-channel block is in "User In" mode (confirmed
raw value 20 on the "rtgin" table). Flipping a block affects all 8
channels in it, not just the one being tested -- this tool prints exactly
what it's about to touch before touching it.

Snapshots (reads) the channel value and its block's value before writing
either, logs everything through DiagnosticsLogger with a correlation_id,
then reads both back to confirm the write matches. Does not revert
automatically -- rerun with the original values (printed in the "Before:"
line) to put things back, e.g.:

    # Test: set channel 9 to Local Analog In 5, flipping its block to User In
    python -m app.tools.test_write_channel --console 10.10.0.142 --channel 9 --value 5

    # Revert: restore channel 9 and its block to what they were before
    python -m app.tools.test_write_channel --console 10.10.0.142 --channel 9 \
        --value 0 --block-value 1
"""
from __future__ import annotations

import argparse
import sys

from app.config import load_config
from app.diagnostics.logger import DiagnosticsLogger
from app.osc import addresses
from app.osc.connection import FirmwareTooOldError, OscConnection, OscConnectionError

USER_IN_VALUE = 20  # confirmed on "rtgin" -- see app.osc.addresses


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.test_write_channel",
        description="Manual test: write one channel's userrout/in value and verify it reads back correctly.",
    )
    parser.add_argument("--console", required=True, help="Console IP address")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--channel", type=int, required=True, help=f"Channel 1-{addresses.NUM_USERROUT_IN}")
    parser.add_argument("--value", type=int, required=True, help="Raw userrout/in value to write")
    parser.add_argument(
        "--block-value",
        type=int,
        default=USER_IN_VALUE,
        help=f"Raw value to write to the channel's containing routing block (default {USER_IN_VALUE} = User In)",
    )
    parser.add_argument("--skip-block-flip", action="store_true", help="Only write the channel value, leave the block alone")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--timeout", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not 1 <= args.channel <= addresses.NUM_USERROUT_IN:
        print(f"ERROR: --channel must be 1-{addresses.NUM_USERROUT_IN}", file=sys.stderr)
        return 2

    config = load_config(args.config)
    config.console_ip = args.console
    if args.port is not None:
        config.console_port = args.port
    if args.log_dir is not None:
        config.log_dir = args.log_dir
    if args.timeout is not None:
        config.osc_timeout_sec = args.timeout

    channel_addr = addresses.userrout_in_addr(args.channel)
    block_index = (args.channel - 1) // 8
    block_addr = addresses.ROUTING_IN_BLOCKS[block_index]
    block_channels = (block_index * 8 + 1, block_index * 8 + 8)

    diagnostics = DiagnosticsLogger(log_dir=config.resolved_log_dir(), ring_buffer_size=config.ring_buffer_size)
    correlation_id = diagnostics.log_user_action(
        "cli_test_write_channel",
        {"channel": args.channel, "value": args.value, "block_addr": block_addr, "console": config.console_ip},
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

        if not args.skip_block_flip:
            print(
                f"NOTE: this will flip the routing block covering channels "
                f"{block_channels[0]}-{block_channels[1]} to raw value {args.block_value} "
                f"({addresses.decode_routing_value('rtgin', args.block_value)}), affecting all "
                f"8 channels in that block, not just channel {args.channel}."
            )

        (original_channel_value,) = osc.query(channel_addr, correlation_id=correlation_id)
        (original_block_value,) = osc.query(block_addr, correlation_id=correlation_id)
        print(
            f"Before: {channel_addr} = {original_channel_value} "
            f"({addresses.decode_userrout_value(original_channel_value)}), "
            f"{block_addr} = {original_block_value} "
            f"({addresses.decode_routing_value('rtgin', original_block_value)})"
        )
        diagnostics.log_state_change(
            "test_write_channel_before",
            after={"channel_value": original_channel_value, "block_value": original_block_value},
            correlation_id=correlation_id,
        )

        print(f"Writing {channel_addr} = {args.value} ...")
        osc.send(channel_addr, args.value, correlation_id=correlation_id)

        if not args.skip_block_flip:
            print(f"Writing {block_addr} = {args.block_value} ...")
            osc.send(block_addr, args.block_value, correlation_id=correlation_id)

        (new_channel_value,) = osc.query(channel_addr, correlation_id=correlation_id)
        (new_block_value,) = osc.query(block_addr, correlation_id=correlation_id)
        print(
            f"After:  {channel_addr} = {new_channel_value} "
            f"({addresses.decode_userrout_value(new_channel_value)}), "
            f"{block_addr} = {new_block_value} "
            f"({addresses.decode_routing_value('rtgin', new_block_value)})"
        )
        diagnostics.log_state_change(
            "test_write_channel_after",
            after={"channel_value": new_channel_value, "block_value": new_block_value},
            correlation_id=correlation_id,
        )

        if new_channel_value != args.value:
            print("MISMATCH: channel value did not read back as written.", file=sys.stderr)
            exit_code = 1
        if not args.skip_block_flip and new_block_value != args.block_value:
            print("MISMATCH: block value did not read back as written.", file=sys.stderr)
            exit_code = 1

        if exit_code == 0:
            print("\nMatch confirmed by readback. Check the console's routing screen now to visually confirm too.")
        print(
            f"\nTo revert, rerun with: --channel {args.channel} --value {original_channel_value} "
            f"--block-value {original_block_value}"
        )

    except FirmwareTooOldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="test_write_channel firmware check failed", correlation_id=correlation_id)
        exit_code = 2
    except OscConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="test_write_channel connection failed", correlation_id=correlation_id)
        exit_code = 1
    except Exception as exc:
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        diagnostics.log_error(exc, context="test_write_channel unexpected failure", correlation_id=correlation_id)
        exit_code = 1
    finally:
        osc.close()
        diagnostics.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
