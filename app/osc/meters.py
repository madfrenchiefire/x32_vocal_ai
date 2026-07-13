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

Slot layouts are now doc-confirmed (Maillot's "Unofficial X32/M32 OSC
Remote Protocol" v4.09, committed in this repo as X32_OSC.pdf, /meters
chapter) and match the empirical captures exactly:

- **/meters/1 (96): slots 0-31 = the 32 channel input meters, 32-63 = 32
  gate gain-reductions, 64-95 = 32 dynamics gain-reductions.** The
  captures showed 0-31 jittering at the analog noise floor (live meters)
  while 32-95 sat constant -- gain-reduction meters with no signal.
- **/meters/2 (49): slots 0-15 = 16 bus masters, 16-21 = 6 matrixes,
  22-23 = main LR, 24 = mono M/C, then 25-48 = the same again as
  dynamics gain-reductions (16 bus + 6 matrix + 1 main + 1 mono).** The
  captures showed exactly slots 25-48 pegged at 1.0 -- GR meters at
  unity.

Other blobs documented in the PDF but not captured yet: /meters/0 (70:
METERS page), /meters/3 (22: aux/fx), /meters/4 (82: in/out), /meters/5-16
(surface VU / strip / sends / FX / monitor / recorder pages; /meters/5
and /meters/6 take extra int args selecting a bank/channel).

The in-app level meters don't depend on any of this either way -- they're
computed from the app's own captured audio (see CLAUDE.md "1. Audio
engine").
"""
from __future__ import annotations

import struct

# /meters/1 layout (doc-confirmed, see module docstring).
METERS1_COUNT = 96
METERS1_CHANNEL_SLOTS = slice(0, 32)
METERS1_GATE_GR_SLOTS = slice(32, 64)
METERS1_DYN_GR_SLOTS = slice(64, 96)

# /meters/2 layout (doc-confirmed).
METERS2_COUNT = 49
METERS2_BUS_SLOTS = slice(0, 16)
METERS2_MATRIX_SLOTS = slice(16, 22)
METERS2_MAIN_LR_SLOTS = slice(22, 24)
METERS2_MONO_SLOT = 24


class MeterBlobError(ValueError):
    pass


def channel_meters(blob: bytes) -> list[float]:
    """The 32 channel input meters (channels 1-32, in order) from one
    /meters/1 blob."""
    values = decode_meter_blob(blob)
    if len(values) != METERS1_COUNT:
        raise MeterBlobError(f"expected a /meters/1 blob of {METERS1_COUNT} values, got {len(values)}")
    return values[METERS1_CHANNEL_SLOTS]


RTA_BIN_COUNT = 100


def decode_rta_blob(blob: bytes) -> list[float]:
    """Decode one /meters/15 RTA blob into 100 dB values (-128.0 .. 0.0).

    Format per X32_OSC.pdf: 50 int32 words packing 100 successive
    little-endian SIGNED short ints; each dB value = short / 256.0 (so
    0x8000 -> -128.0 dB, 0xffff -> -0.004 dB, and exactly 0x0000 = 0.0 dB
    means clipping). Tolerates the same leading little-endian int32 count
    word the /meters/1 and /meters/2 blobs carry (confirmed on real
    captures), in case /meters/15 shares it."""
    if len(blob) >= 4:
        (maybe_count,) = struct.unpack("<i", blob[:4])
        if maybe_count == 50 and len(blob) == 4 + RTA_BIN_COUNT * 2:
            blob = blob[4:]
    if len(blob) != RTA_BIN_COUNT * 2:
        raise MeterBlobError(
            f"RTA blob is {len(blob)} bytes; expected {RTA_BIN_COUNT * 2} (or +4 with a count prefix)"
        )
    shorts = struct.unpack(f"<{RTA_BIN_COUNT}h", blob)
    return [s / 256.0 for s in shorts]


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
