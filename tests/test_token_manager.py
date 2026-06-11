"""Token expiry parsing — IST/UTC handling that the whole refresh cycle hangs on."""
from datetime import datetime, timezone

from core.token_manager import _parse_expiry


def test_parses_utc_z_suffix():
    dt = _parse_expiry("2026-06-12T10:00:00Z")
    assert dt == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_parses_naive_as_ist():
    dt = _parse_expiry("2026-06-12T15:30:00")
    # 15:30 IST == 10:00 UTC
    assert dt == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_empty_and_garbage_return_none():
    assert _parse_expiry("") is None
    assert _parse_expiry("not-a-date") is None
