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

**Slot semantics, from comparing two captures taken ~19 minutes apart
(same console, no signal during either capture's meters window):**

- **/meters/1 slots 0-31 are the 32 live channel input meters** -- every
  one of the 32 shows tiny per-blob variance at the analog noise floor
  (~1.4e-5 ~= -97 dBFS, a different value in every 50 ms blob, in both
  captures independently), which is exactly what idle preamp inputs look
  like and cannot be produced by a static parameter. A final 1:1
  index->channel confirmation (signal on exactly one known channel) is
  still worth doing before trusting any *specific* index, but the region
  is unambiguous.
- **/meters/1 slots 32-95 are NOT audio meters** -- they were exactly
  constant within each capture AND bit-identical across both captures,
  sitting at suspiciously round dB values (0.0891 ~= -21 dB, 0.3162 =
  -10 dB, 0.1 = -20 dB, 1.0 = 0 dB). Whatever the console packs in there
  (thresholds/gains/fader-like parameters), it doesn't move with audio;
  do not read those slots as levels.

The in-app level meters don't depend on any of this either way -- they're
computed from the app's own captured audio (see CLAUDE.md "1. Audio
engine").
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
