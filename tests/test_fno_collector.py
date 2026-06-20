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
    """run_eod_collection must return a dict with the documented keys.

    The two backfill_index_bars calls return DISTINCT values (NIFTY→5, VIX→9)
    so the routing into index_bars vs vix_bars is actually verified.
    """
    client = _make_patched_client(_make_expiry_list([], FUTURE_EXPIRIES[:2]))

    # side_effect list: first call (NIFTY) → 5, second call (INDIAVIX) → 9.
    mock_backfill = AsyncMock(side_effect=[5, 9])
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
        "index_bars", "vix_bars", "expiries", "snapshots", "chain_rows", "atm", "errors"
    }
    # NIFTY bars land in index_bars, VIX bars land in vix_bars.
    assert result["index_bars"] == 5
    assert result["vix_bars"] == 9
    assert result["expiries"] == 4
    assert result["errors"] == []  # happy path: no step failed


def test_index_bars_failure_does_not_abort_collection():
    """A transient failure of one step (e.g. Dhan's daily charts endpoint) must be
    isolated: the error is recorded and the other steps still run."""
    client = _make_patched_client(_make_expiry_list([], FUTURE_EXPIRIES))
    # NIFTY backfill raises (daily endpoint down); VIX/calendar/snapshots succeed.
    mock_backfill = AsyncMock(side_effect=[RuntimeError("DH-905 daily down"), 9])
    mock_calendar = AsyncMock(return_value=4)
    mock_snapshot = AsyncMock(return_value={"chain_rows": 80, "atm": 1})
    with (
        patch.object(collector.fb, "backfill_index_bars", mock_backfill),
        patch.object(collector.fb, "build_expiry_calendar", mock_calendar),
        patch.object(collector.fb, "snapshot_option_chain", mock_snapshot),
    ):
        result = _run(collector.run_eod_collection(client, "NIFTY", n_expiries=2, now=_NOW))
    assert result["index_bars"] == 0          # failed step → default
    assert result["vix_bars"] == 9            # other steps still ran
    assert result["expiries"] == 4
    assert result["snapshots"] == 2
    assert any("index_bars" in e for e in result["errors"])  # failure recorded, not raised


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
    expected_from = (_TODAY - timedelta(days=lookback)).isoformat()

    for call in mock_backfill.call_args_list:
        # Positional: (client, security_id, symbol, from_date, to_date, ...)
        from_arg = call.args[3] if len(call.args) > 3 else call.kwargs.get("from_date")
        to_arg = call.args[4] if len(call.args) > 4 else call.kwargs.get("to_date")
        assert from_arg == expected_from
        assert to_arg == expected_to


# ── main() coverage ───────────────────────────────────────────────────────────

def _make_main_patches(*, token: Any = "tok123", with_instruments: bool = False):
    """Return a context manager that patches all deferred imports used by main().

    The returned mapping exposes the individual mocks so tests can assert on them.
    """
    import contextlib
    import sys

    @contextlib.contextmanager
    def _ctx():
        argv = ["fno_collector"]
        if with_instruments:
            argv.append("--with-instruments")

        mock_cfg = MagicMock()
        mock_cfg.db_url = "postgresql://fake/db"
        mock_cfg.dhan_client_id = "CLIENT1"

        mock_get_config = MagicMock(return_value=mock_cfg)
        mock_init_db = MagicMock()
        # main() now resolves the token inside the async _run via
        # fb.resolve_access_token(); patch that (read_current_token is its first
        # source). mock_token stands in for the cached read.
        mock_token = MagicMock(return_value=token)
        mock_resolve = AsyncMock(return_value=token)
        mock_client_cls = MagicMock()
        mock_client_instance = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_rv_fn = MagicMock(return_value=42)
        mock_sync_instruments = MagicMock(return_value={"inserted": 0, "updated": 0})
        _summary = {"index_bars": 1, "vix_bars": 1,
                    "expiries": 2, "snapshots": 1,
                    "chain_rows": 10, "atm": 1}

        def _fake_asyncio_run(coro):
            """Close the coroutine (suppress 'never awaited' warning) and return stub."""
            coro.close()
            return _summary

        mock_asyncio_run = MagicMock(side_effect=_fake_asyncio_run)
        mock_record_paper = MagicMock(return_value={"recorded": False, "decision": "STAND_ASIDE"})
        mock_resolve_paper = MagicMock(return_value=0)

        with (
            patch.object(sys, "argv", argv),
            patch("core.fno_collector.compute_index_realized_vol", mock_rv_fn),
            patch("asyncio.run", mock_asyncio_run),
            # main() does `import core.fno_instruments as fno_instruments` / `from core.fno_paper
            # import ...`, which bind via the real module — so patch the real attributes (not
            # sys.modules), or the real functions run (download the master / hit the DB).
            patch("core.fno_instruments.sync_fno_instruments", mock_sync_instruments),
            patch("core.fno_paper.record_paper_entry", mock_record_paper),
            patch("core.fno_paper.resolve_paper_trades", mock_resolve_paper),
        ):
            # config/db/core.client/core.token_manager are reached via `from X import Y`,
            # which resolves through sys.modules — so swapping those is sufficient.
            with (
                patch.dict("sys.modules", {
                    "config": MagicMock(get_config=mock_get_config),
                    "db": MagicMock(init_db=mock_init_db),
                    "core.client": MagicMock(DhanClient=mock_client_cls),
                    "core.token_manager": MagicMock(read_current_token=mock_token),
                }),
            ):
                mocks = {
                    "get_config": mock_get_config,
                    "init_db": mock_init_db,
                    "read_current_token": mock_token,
                    "resolve_access_token": mock_resolve,
                    "DhanClient": mock_client_cls,
                    "compute_index_realized_vol": mock_rv_fn,
                    "asyncio_run": mock_asyncio_run,
                    "sync_fno_instruments": mock_sync_instruments,
                    "record_paper_entry": mock_record_paper,
                    "resolve_paper_trades": mock_resolve_paper,
                }
                yield mocks

    return _ctx()


def test_main_with_instruments_calls_sync_once():
    """--with-instruments must call sync_fno_instruments exactly once."""
    with _make_main_patches(token="tok", with_instruments=True) as mocks:
        import core.fno_collector as _col
        _col.main()
        mocks["sync_fno_instruments"].assert_called_once()


def test_main_without_instruments_does_not_call_sync():
    """Without --with-instruments, sync_fno_instruments must NOT be called."""
    with _make_main_patches(token="tok", with_instruments=False) as mocks:
        import core.fno_collector as _col
        _col.main()
        mocks["sync_fno_instruments"].assert_not_called()


def test_main_runs_paper_log():
    """main() resolves matured paper trades and records today's entry once."""
    with _make_main_patches(token="tok", with_instruments=False) as mocks:
        import core.fno_collector as _col
        _col.main()
        mocks["resolve_paper_trades"].assert_called_once()
        mocks["record_paper_entry"].assert_called_once()


def test_main_resolves_token_inside_run():
    """main() no longer hard-fails on an empty cache: token resolution moved into
    the async _run (via fb.resolve_access_token), which falls back to PIN/TOTP.
    main() must still drive the collection (asyncio.run is called) regardless of
    the cache state — the cache→generate fallback is exercised in the
    fno_backfill resolve_access_token tests."""
    with _make_main_patches(token=None, with_instruments=False) as mocks:
        import core.fno_collector as _col
        _col.main()
        # _run is driven via asyncio.run even when the static cache is empty.
        mocks["asyncio_run"].assert_called_once()
