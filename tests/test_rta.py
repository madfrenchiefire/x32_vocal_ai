from __future__ import annotations

import struct
import threading
import time

import pytest

from app.osc.connection import OscConnection
from app.osc.meters import MeterBlobError, decode_rta_blob
from app.osc.rta import RTA_SOURCE_ADDRESS, RtaStreamer, rta_source_for_channel


def _make_osc(fake_x32, diagnostics, app_state) -> OscConnection:
    osc = OscConnection(
        host="127.0.0.1",
        port=fake_x32.port,
        diagnostics=diagnostics,
        state=app_state,
        timeout_sec=1.0,
        xremote_interval_sec=0.5,
    )
    osc.connect()
    return osc


def _rta_blob(db_values: list[float]) -> bytes:
    shorts = [int(round(db * 256.0)) for db in db_values]
    return struct.pack(f"<{len(shorts)}h", *shorts)


# -- decoder -------------------------------------------------------------------


def test_decode_rta_blob_doc_examples():
    # Straight from X32_OSC.pdf: word 008000c0 -> -128.0 and -64.0 dB;
    # word 40e0ffff -> -31.75 and ~-0.004 dB.
    values = decode_rta_blob(bytes.fromhex("008000c0") + _rta_blob([0.0] * 98))
    assert values[0] == pytest.approx(-128.0)
    assert values[1] == pytest.approx(-64.0)

    values = decode_rta_blob(bytes.fromhex("40e0ffff") + _rta_blob([0.0] * 98))
    assert values[0] == pytest.approx(-31.75)
    assert values[1] == pytest.approx(-1 / 256.0)


def test_decode_rta_blob_roundtrip_and_count_prefix():
    dbs = [-(i % 97) - 0.25 for i in range(100)]
    raw = _rta_blob(dbs)
    assert decode_rta_blob(raw) == pytest.approx(dbs)
    # Same blob with the /meters-style leading int32 count word.
    assert decode_rta_blob(struct.pack("<i", 50) + raw) == pytest.approx(dbs)


def test_decode_rta_blob_rejects_wrong_size():
    with pytest.raises(MeterBlobError):
        decode_rta_blob(b"\x00" * 42)


def test_rta_source_for_channel():
    assert rta_source_for_channel(1) == 0
    assert rta_source_for_channel(32) == 31
    with pytest.raises(ValueError):
        rta_source_for_channel(33)


# -- streamer ------------------------------------------------------------------


def test_rta_streamer_decodes_pushed_blobs(fake_x32, diagnostics, app_state):
    dbs = [-24.0] * 100
    fake_x32.extra_responses["/meters/15"] = (_rta_blob(dbs),)
    osc = _make_osc(fake_x32, diagnostics, app_state)

    frames: list = []
    streamer = RtaStreamer(osc, diagnostics, on_rta=frames.append, renew_sec=5.0)

    def push_blob():
        time.sleep(0.15)
        osc.send("/meters/15")  # bare get -> fake replies with the blob, arrives like a push

    try:
        streamer.start()
        threading.Thread(target=push_blob, daemon=True).start()
        deadline = time.monotonic() + 2.0
        while not frames and time.monotonic() < deadline:
            time.sleep(0.05)
        streamer.stop()
    finally:
        osc.close()

    assert frames and frames[0] == pytest.approx(dbs)
    # The subscribe went to the parent /meters address in the documented form.
    assert fake_x32.extra_responses["/meters"] == ("/meters/15",)


def test_rta_streamer_sets_and_restores_source(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[RTA_SOURCE_ADDRESS] = (70,)  # console RTA on Main L/R
    osc = _make_osc(fake_x32, diagnostics, app_state)
    streamer = RtaStreamer(osc, diagnostics, on_rta=lambda bins: None, renew_sec=5.0)
    try:
        streamer.start(source=rta_source_for_channel(9))
        time.sleep(0.1)
        assert fake_x32.extra_responses[RTA_SOURCE_ADDRESS] == (8,)
        streamer.stop()
        time.sleep(0.1)
        assert fake_x32.extra_responses[RTA_SOURCE_ADDRESS] == (70,)
    finally:
        osc.close()


def test_rta_streamer_leaves_source_alone_when_not_asked(fake_x32, diagnostics, app_state):
    fake_x32.extra_responses[RTA_SOURCE_ADDRESS] = (70,)
    osc = _make_osc(fake_x32, diagnostics, app_state)
    streamer = RtaStreamer(osc, diagnostics, on_rta=lambda bins: None, renew_sec=5.0)
    try:
        streamer.start()  # no source
        time.sleep(0.1)
        streamer.stop()
        assert fake_x32.extra_responses[RTA_SOURCE_ADDRESS] == (70,)
    finally:
        osc.close()
