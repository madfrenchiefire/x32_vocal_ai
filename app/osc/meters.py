"""X32 /meters blob decoding.

Blob *structure* confirmed against real hardware (2026-07-07, firmware
4.13) from a raw capture taken by `python -m app.tools.diagnose_console`:
subscribing via `/meters ,s "/meters/1"` streams one OSC blob every ~50 ms
for a few seconds, and each blob is

    int32 count (LITTLE-endian) + count * float32 (LITTLE-endian)

with every float a 0..1 linear meter value. Note the endianness: OSC's own
wire format is big-endian, but the X32 encodes the blob *payload*
little-endian (an idiosyncrasy Maillot's unofficial doc also describes) --
confirmed on the real capture, where the leading int32 reads 96 as LE and
garbage as BE, and 60 consecutive blobs parse cleanly at the implied size
with zero bytes left over.

Confirmed counts: /meters/1 carries 96 values, /meters/2 carries 49.

**What each slot means is NOT yet mapped.** The same capture shows slots
0-31 of /meters/1 all zero while the console's 32 input channels were
silent, and activity in the mid-30s slot range while a stereo USB-player
signal was live on Aux 7/8 -- consistent with slots 0-31 being channels
1-32 followed by the 8 Aux ins, but that's one uncontrolled observation,
not a mapping. Confirm with a controlled test (signal on exactly one known
channel; see which slot moves) before relying on any slot index. The
in-app level meters don't depend on this either way -- they're computed
from the app's own captured audio (see CLAUDE.md "1. Audio engine").
"""
from __future__ import annotations

import struct


class MeterBlobError(ValueError):
    pass


def decode_meter_blob(blob: bytes) -> list[float]:
    """Decode one /meters/N OSC blob into its list of 0..1 floats.

    Raises MeterBlobError if the leading count doesn't exactly match the
    blob's size -- the count/size relationship is the only structural
    invariant available, so a mismatch means either a truncated packet or
    a blob that isn't in this confirmed format."""
    if len(blob) < 4:
        raise MeterBlobError(f"blob too short to contain a count: {len(blob)} byte(s)")
    (count,) = struct.unpack("<i", blob[:4])
    expected_size = 4 + count * 4
    if count <= 0 or len(blob) != expected_size:
        raise MeterBlobError(
            f"blob size {len(blob)} does not match leading count {count} (expected {expected_size})"
        )
    return list(struct.unpack(f"<{count}f", blob[4:]))
