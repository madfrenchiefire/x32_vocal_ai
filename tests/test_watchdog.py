from __future__ import annotations

import pytest

from app.watchdog import Watchdog


def test_watchdog_is_not_yet_implemented():
    with pytest.raises(NotImplementedError):
        Watchdog(state=None, diagnostics=None)
