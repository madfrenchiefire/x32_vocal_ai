from __future__ import annotations

import pytest

from app.osc.routing_apply import apply_routing, bypass_channel, restore_snapshot


def test_apply_routing_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        apply_routing(osc=None, diagnostics=None, selected_channels=[1], snapshot=None)


def test_bypass_channel_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        bypass_channel(osc=None, diagnostics=None, channel=1, snapshot=None)


def test_restore_snapshot_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        restore_snapshot(osc=None, diagnostics=None, snapshot=None)
