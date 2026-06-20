"""Tests for core/fno_equity_collector.py — no network, no DB.

The Dhan client and fno_backfill orchestration functions are mocked. Covers:
  • backfill_equity_futures loops the universe, calls backfill_futures_bars with
    instrument="FUTSTK" + "<SYM>-FUT", sums rows, tolerates per-stock failure.
  • collect_equity_chains loops the universe, resolves per-stock expiries via
    get_fno_expiry_list(..., "NSE_FNO"), snapshots with underlying_seg="NSE_FNO",
    throttles between chain calls, and one stock failing does NOT abort the rest.
  • top_n restricts the universe subset.
  • main() resolves the token via fb.resolve_access_token (never the static env token).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import core.fno_backfill as fb
import core.fno_equity_collector as ec

_IST = fb._IST
_NOW = datetime(2026, 6, 20, 18, 0, tzinfo=_IST)   # Sat 18:00 IST — off-hours
_TODAY = _NOW.date()


_UNIVERSE = [
    {"underlying_symbol": "RELIANCE", "underlying_security_id": "2885",
     "future_security_id": "61001", "near_expiry": date(2026, 6, 26), "lot_size": 500},
    {"underlying_symbol": "TCS", "underlying_security_id": "11536",
     "future_security_id": "61010", "near_expiry": date(2026, 6, 26), "lot_size": 175},
    {"underlying_symbol": "INFY", "underlying_security_id": "1594",
     "future_security_id": "61020", "near_expiry": date(2026, 6, 26), "lot_size": 400},
]


def _run(coro):
    return asyncio.run(coro)


class _ExpiryClient:
    """Fake client whose expiry list yields one future expiry per stock."""

    def __init__(self, expiries=None):
        self._expiries = expiries or ["2026-06-26", "2026-07-31"]
        self.expiry_calls = []

    async def get_fno_expiry_list(self, scrip, seg="IDX_I"):
        self.expiry_calls.append((scrip, seg))
        return {"data": self._expiries}


# ── futures backfill driver ──────────────────────────────────────────────────────
def test_backfill_equity_futures_loops_universe_with_futstk():
    mock_bf = AsyncMock(return_value=7)
    with (
        patch.object(ec, "load_universe", lambda: list(_UNIVERSE)),
        patch.object(ec.fb, "backfill_futures_bars", mock_bf),
    ):
        res = _run(ec.backfill_equity_futures(
            object(), "2024-06-01", "2026-06-18", now=_NOW,
        ))

    assert mock_bf.call_count == 3
    assert res["symbols"] == 3
    assert res["ok"] == 3
    assert res["rows"] == 21          # 7 * 3
    assert res["errors"] == []

    # Each call must use instrument=FUTSTK and the "<SYM>-FUT" symbol.
    instruments = {c.kwargs.get("instrument") for c in mock_bf.call_args_list}
    assert instruments == {"FUTSTK"}
    symbols = {c.args[1] for c in mock_bf.call_args_list}
    assert symbols == {"RELIANCE-FUT", "TCS-FUT", "INFY-FUT"}


def test_backfill_equity_futures_tolerates_per_stock_failure():
    # TCS (second call) raises; others succeed.
    mock_bf = AsyncMock(side_effect=[5, RuntimeError("DH-905"), 5])
    with (
        patch.object(ec, "load_universe", lambda: list(_UNIVERSE)),
        patch.object(ec.fb, "backfill_futures_bars", mock_bf),
    ):
        res = _run(ec.backfill_equity_futures(object(), "a", "b", now=_NOW))

    assert mock_bf.call_count == 3       # the failure did not abort the loop
    assert res["ok"] == 2
    assert res["rows"] == 10
    assert any("TCS" in e for e in res["errors"])


def test_backfill_equity_futures_top_n_subset():
    mock_bf = AsyncMock(return_value=1)
    with (
        patch.object(ec, "load_universe", lambda: list(_UNIVERSE)),
        patch.object(ec.fb, "backfill_futures_bars", mock_bf),
    ):
        res = _run(ec.backfill_equity_futures(object(), "a", "b", top_n=2, now=_NOW))
    assert mock_bf.call_count == 2
    assert res["symbols"] == 2


# ── option-chain forward collector ───────────────────────────────────────────────
def test_collect_equity_chains_loops_universe_nse_fno():
    client = _ExpiryClient()
    mock_snap = AsyncMock(return_value={"chain_rows": 40, "atm": 1})
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    with (
        patch.object(ec, "load_universe", lambda: list(_UNIVERSE)),
        patch.object(ec.fb, "snapshot_option_chain", mock_snap),
    ):
        res = _run(ec.collect_equity_chains(
            client, n_expiries=1, throttle_s=3.0, sleep=_fake_sleep, now=_NOW,
        ))

    assert res["stocks"] == 3
    assert res["ok"] == 3
    assert res["chain_rows"] == 120       # 40 * 3
    assert res["atm"] == 3

    # Expiry list resolved per stock with the NSE_FNO segment.
    assert all(seg == "NSE_FNO" for _, seg in client.expiry_calls)
    assert {scrip for scrip, _ in client.expiry_calls} == {2885, 11536, 1594}

    # snapshot called with underlying_seg=NSE_FNO and the int scrip.
    for call in mock_snap.call_args_list:
        assert call.kwargs.get("underlying_seg") == "NSE_FNO"
        assert call.kwargs.get("expiry_date") == date(2026, 6, 26)

    # Throttle applied BETWEEN calls (3 calls → 2 sleeps), each == 3.0s.
    assert sleeps == [3.0, 3.0]


def test_collect_equity_chains_tolerates_per_stock_failure():
    client = _ExpiryClient()

    # Second stock's snapshot raises; others succeed.
    def _snap_side_effect(*args, **kwargs):
        n = mock_snap.call_count
        if n == 2:
            raise RuntimeError("chain fetch down")

        async def _ok():
            return {"chain_rows": 10, "atm": 1}
        return _ok()

    mock_snap = MagicMock(side_effect=_snap_side_effect)

    async def _no_sleep(s):
        return None

    with (
        patch.object(ec, "load_universe", lambda: list(_UNIVERSE)),
        patch.object(ec.fb, "snapshot_option_chain", mock_snap),
    ):
        res = _run(ec.collect_equity_chains(
            client, n_expiries=1, sleep=_no_sleep, now=_NOW,
        ))

    assert mock_snap.call_count == 3       # loop continued past the failure
    assert res["ok"] == 2                  # one stock failed
    assert res["chain_rows"] == 20         # two successes * 10
    assert any("TCS" in e for e in res["errors"])


def test_collect_equity_chains_skips_stock_with_no_future_expiry():
    # All expiries are in the past → that stock yields no snapshot.
    client = _ExpiryClient(expiries=["2020-01-01"])
    mock_snap = AsyncMock(return_value={"chain_rows": 5, "atm": 1})

    async def _no_sleep(s):
        return None

    with (
        patch.object(ec, "load_universe", lambda: list(_UNIVERSE)),
        patch.object(ec.fb, "snapshot_option_chain", mock_snap),
    ):
        res = _run(ec.collect_equity_chains(client, sleep=_no_sleep, now=_NOW))

    assert mock_snap.call_count == 0
    assert res["chain_rows"] == 0
    assert res["ok"] == 0


# ── main() token path ─────────────────────────────────────────────────────────────
def test_main_resolves_token_and_runs_chains():
    """main --chains must resolve the token via fb.resolve_access_token (managed),
    never the static env token, and drive collect_equity_chains."""
    mock_cfg = MagicMock()
    mock_cfg.db_url = "postgresql://fake/db"
    mock_cfg.dhan_client_id = "CLIENT1"
    mock_cfg.dhan_access_token = "STATIC-EXPIRED"

    mock_client_cls = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_resolve = AsyncMock(return_value="MANAGED")
    mock_collect = AsyncMock(return_value={"stocks": 1, "ok": 1, "chain_rows": 1, "atm": 1, "errors": []})

    with (
        patch.object(sys, "argv", ["fno_equity_collector", "--chains", "--top-n", "5"]),
        patch.object(fb, "resolve_access_token", mock_resolve),
        patch("core.fno_equity_collector.collect_equity_chains", mock_collect),
        patch.dict("sys.modules", {
            "config": MagicMock(get_config=MagicMock(return_value=mock_cfg)),
            "db": MagicMock(init_db=MagicMock()),
            "core.client": MagicMock(DhanClient=mock_client_cls),
        }),
    ):
        ec.main()

    mock_resolve.assert_awaited_once()
    mock_collect.assert_awaited_once()
    pos, kw = mock_client_cls.call_args
    passed = list(pos) + list(kw.values())
    assert "MANAGED" in passed
    assert "STATIC-EXPIRED" not in passed
