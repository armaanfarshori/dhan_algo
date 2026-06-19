"""Tests for core/fno_collector.py — no DB, no network.

All Dhan client calls and fno_backfill orchestration functions are replaced
with AsyncMock / regular fakes.  Verifies:

  • backfill_index_bars is called for BOTH nifty_id and vix_id with the
    correct symbol strings.
  • snapshot_option_chain is called only for the FUTURE expiries that fall
    within the nearest n_expiries (past dates are skipped).
  • chain_rows and atm values are correctly summed across snapshots.
  • The summary dict has all expected keys.
  • A snapshot raising for one expiry does not abort the others — remaining
    snapshots still run and their results are accumulated.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import core.fno_collector as collector
import core.fno_backfill as fb

# A fixed "now" in IST that is off-hours (Saturday evening) so the
# off-hours guard in fno_backfill does not fire.
_IST = fb._IST
_NOW = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)   # Sat 18:00 IST — market closed
_TODAY = _NOW.date()                                  # 2026-06-20


def _make_expiry_list(past: list[date], future: list[date]) -> list[str]:
    """Build a raw list of ISO date strings mixing past and future dates."""
    return [d.isoformat() for d in sorted(past + future)]


class FakeClient:
    """Minimal fake DhanClient — sync callable, not an async context manager.

    ``get_fno_expiry_list`` returns a canned list; all other methods are stubs.
    The collector never calls __aenter__/__aexit__ on the client directly —
    it receives an already-open client instance.
    """

    def __init__(self, expiry_dates: list[str]):
        self._expiry_dates = expiry_dates

    async def get_fno_expiry_list(self, scrip: int, seg: str = "IDX_I") -> dict[str, Any]:
        return {"data": self._expiry_dates}


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    """Run a coroutine synchronously (avoids the pytest-asyncio dependency)."""
    return asyncio.run(coro)


# ── fixtures ──────────────────────────────────────────────────────────────────

PAST_EXPIRIES = [
    _TODAY - timedelta(days=14),   # 2 weeks ago
    _TODAY - timedelta(days=7),    # 1 week ago
]
FUTURE_EXPIRIES = [
    _TODAY + timedelta(days=3),    # nearest
    _TODAY + timedelta(days=10),
    _TODAY + timedelta(days=17),
    _TODAY + timedelta(days=24),
    _TODAY + timedelta(days=31),   # 5th future — should be excluded with n_expiries=4
]


def _make_patched_client(expiry_dates: list[str]) -> FakeClient:
    return FakeClient(expiry_dates)


# ── core correctness tests ─────────────────────────────────────────────────────

def test_backfill_called_for_both_index_ids():
    """backfill_index_bars must be awaited for both nifty_id and vix_id."""
    client = _make_patched_client(_make_expiry_list(PAST_EXPIRIES, FUTURE_EXPIRIES))

    mock_backfill = AsyncMock(return_value=5)
    mock_calendar = AsyncMock(return_value=10)
    mock_snapshot = AsyncMock(return_value={"chain_rows": 100, "atm": 1})

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        _run(
            collector.run_eod_collection(
                client, "NIFTY", nifty_id="13", vix_id="21",
                lookback_days=10, n_expiries=4, now=_NOW,
            )
        )

    # Must be called exactly twice (once per index id).
    assert mock_backfill.call_count == 2

    calls = mock_backfill.call_args_list
    symbols_called = {c.args[2] if len(c.args) >= 3 else c.kwargs.get("symbol") for c in calls}
    sec_ids_called = {c.args[1] if len(c.args) >= 2 else c.kwargs.get("security_id") for c in calls}

    assert "NIFTY" in symbols_called
    assert "INDIAVIX" in symbols_called
    assert "13" in sec_ids_called
    assert "21" in sec_ids_called


def test_snapshot_only_future_expiries_up_to_n():
    """snapshot_option_chain must be called only for the nearest n_expiries
    FUTURE dates — past dates and expiries beyond n must be skipped."""
    n = 4
    client = _make_patched_client(_make_expiry_list(PAST_EXPIRIES, FUTURE_EXPIRIES))

    mock_backfill = AsyncMock(return_value=1)
    mock_calendar = AsyncMock(return_value=5)
    mock_snapshot = AsyncMock(return_value={"chain_rows": 50, "atm": 1})

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        _run(
            collector.run_eod_collection(
                client, "NIFTY", n_expiries=n, now=_NOW,
            )
        )

    # Must be called exactly n times (not len(PAST_EXPIRIES) + len(FUTURE_EXPIRIES)).
    assert mock_snapshot.call_count == n

    # All snapshot calls must have expiry_date >= today.
    for call in mock_snapshot.call_args_list:
        expiry_passed = call.kwargs.get("expiry_date") or call.args[2]
        assert expiry_passed >= _TODAY, f"Snapshot called for past expiry: {expiry_passed}"


def test_chain_rows_and_atm_summed_correctly():
    """chain_rows and atm in the summary must be the sum across all snapshots."""
    n = 3
    client = _make_patched_client(_make_expiry_list(PAST_EXPIRIES, FUTURE_EXPIRIES))

    # Each snapshot call returns a different count.
    side_effects = [
        {"chain_rows": 10, "atm": 1},
        {"chain_rows": 20, "atm": 0},
        {"chain_rows": 30, "atm": 1},
    ]
    mock_backfill = AsyncMock(return_value=2)
    mock_calendar = AsyncMock(return_value=3)
    mock_snapshot = AsyncMock(side_effect=side_effects)

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        result = _run(
            collector.run_eod_collection(
                client, "NIFTY", n_expiries=n, now=_NOW,
            )
        )

    assert result["chain_rows"] == 60   # 10 + 20 + 30
    assert result["atm"] == 2           # 1 + 0 + 1
    assert result["snapshots"] == n


def test_summary_dict_has_all_keys():
    """run_eod_collection must return a dict with the documented keys."""
    client = _make_patched_client(_make_expiry_list([], FUTURE_EXPIRIES[:2]))

    mock_backfill = AsyncMock(return_value=7)
    mock_calendar = AsyncMock(return_value=4)
    mock_snapshot = AsyncMock(return_value={"chain_rows": 80, "atm": 1})

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        result = _run(
            collector.run_eod_collection(
                client, "NIFTY", n_expiries=2, now=_NOW,
            )
        )

    assert set(result.keys()) == {
        "index_bars", "vix_bars", "expiries", "snapshots", "chain_rows", "atm"
    }
    assert result["index_bars"] == 7
    assert result["vix_bars"] == 7
    assert result["expiries"] == 4


def test_one_failing_snapshot_does_not_abort_others():
    """If one snapshot raises, the remaining expiries must still be processed
    and their results accumulated.  The failing expiry is just warned and skipped."""
    n = 4
    client = _make_patched_client(_make_expiry_list([], FUTURE_EXPIRIES))

    # Second call raises; others succeed.
    def _snapshot_side_effect(*args, **kwargs):
        call_num = mock_snapshot.call_count  # already incremented before side_effect
        if call_num == 2:
            raise RuntimeError("Simulated chain fetch failure")
        # Return a coroutine that yields the result.
        async def _ok():
            return {"chain_rows": 15, "atm": 1}
        return _ok()

    mock_backfill = AsyncMock(return_value=3)
    mock_calendar = AsyncMock(return_value=6)
    mock_snapshot = MagicMock(side_effect=_snapshot_side_effect)

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        result = _run(
            collector.run_eod_collection(
                client, "NIFTY", n_expiries=n, now=_NOW,
            )
        )

    # 4 calls attempted, 1 failed → 3 succeeded.
    assert mock_snapshot.call_count == n
    assert result["snapshots"] == n - 1
    assert result["chain_rows"] == 15 * (n - 1)
    assert result["atm"] == n - 1


def test_no_future_expiries_skips_snapshots():
    """When all available expiries are in the past, no snapshot should be taken."""
    client = _make_patched_client(_make_expiry_list(PAST_EXPIRIES, []))

    mock_backfill = AsyncMock(return_value=1)
    mock_calendar = AsyncMock(return_value=2)
    mock_snapshot = AsyncMock(return_value={"chain_rows": 99, "atm": 1})

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        result = _run(
            collector.run_eod_collection(
                client, "NIFTY", n_expiries=4, now=_NOW,
            )
        )

    assert mock_snapshot.call_count == 0
    assert result["chain_rows"] == 0
    assert result["atm"] == 0
    assert result["snapshots"] == 0


def test_lookback_dates_passed_to_backfill():
    """The from/to date strings sent to backfill_index_bars must span exactly
    ``lookback_days`` calendar days ending on today."""
    lookback = 7
    client = _make_patched_client(_make_expiry_list([], FUTURE_EXPIRIES[:1]))

    mock_backfill = AsyncMock(return_value=0)
    mock_calendar = AsyncMock(return_value=0)
    mock_snapshot = AsyncMock(return_value={"chain_rows": 0, "atm": 0})

    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        _run(
            collector.run_eod_collection(
                client, "NIFTY", lookback_days=lookback, n_expiries=1, now=_NOW,
            )
        )

    expected_to = _TODAY.isoformat()
    expected_from = (_TODAY - __import__("datetime").timedelta(days=lookback)).isoformat()

    for call in mock_backfill.call_args_list:
        # Positional: (client, security_id, symbol, from_date, to_date, ...)
        from_arg = call.args[3] if len(call.args) > 3 else call.kwargs.get("from_date")
        to_arg = call.args[4] if len(call.args) > 4 else call.kwargs.get("to_date")
        assert from_arg == expected_from
        assert to_arg == expected_to
