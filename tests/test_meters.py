from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app.osc.meters import MeterBlobError, decode_meter_blob


def _make_blob(values: list[float]) -> bytes:
    return struct.pack("<i", len(values)) + struct.pack(f"<{len(values)}f", *values)


def test_decode_meter_blob_roundtrip():
    values = [0.0, 0.25, 0.5, 1.0]
    decoded = decode_meter_blob(_make_blob(values))
    assert decoded == pytest.approx(values)


def test_decode_meter_blob_rejects_truncated_blob():
    blob = _make_blob([0.1, 0.2, 0.3])[:-2]
    with pytest.raises(MeterBlobError):
        decode_meter_blob(blob)


def test_decode_meter_blob_rejects_count_mismatch():
    blob = struct.pack("<i", 5) + struct.pack("<2f", 0.1, 0.2)
    with pytest.raises(MeterBlobError):
        decode_meter_blob(blob)


def test_decode_meter_blob_rejects_tiny_input():
    with pytest.raises(MeterBlobError):
        decode_meter_blob(b"\x01")


def test_decode_meter_blob_against_real_capture_if_present():
    # The raw capture that confirmed this format (real console, firmware
    # 4.13) is committed under logs/protocol_discovery -- 60 concatenated
    # /meters/1 blobs of 96 floats each. Skip rather than fail if the log
    # file has been cleaned up; the synthetic tests above cover the logic.
    path = Path(__file__).parent.parent / "logs" / "protocol_discovery" / "meters_meters_1_20260707T221452Z.bin"
    if not path.exists():
        pytest.skip("real meters capture not present")
    data = path.read_bytes()
    blob_size = 4 + 96 * 4
    assert len(data) % blob_size == 0
    for offset in range(0, len(data), blob_size):
        values = decode_meter_blob(data[offset:offset + blob_size])
        assert len(values) == 96
        assert all(0.0 <= v <= 1.0 for v in values)
