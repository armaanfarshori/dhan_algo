"""
Unit tests for strategies/scalper_screener.py — the contract screener.

Tests are PURE: no DB, no network, no wall-clock dependence. Chain rows are
hand-built dicts that mirror ``core.fno_backfill.parse_option_chain`` VERBATIM.

DATE-DETERMINISM (ci-ist-date-trap)
-----------------------------------
``now`` and ``expiry_date`` are built from a FIXED past trading day, never the
wall clock, so the suite is identical on the IST dev Mac and the UTC CI runner.
The screener does not read ``datetime.now`` itself (``now`` is injected), so
there is no skew coupling, but we keep everything fixed for hygiene. Run
``TZ=UTC pytest tests/test_scalper_screener.py`` to confirm.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from strategies.scalper_screener import (
    ChosenContract,
    ScalperScreener,
    ScreenerInputs,
    ScreenerParams,
)

IST = ZoneInfo("Asia/Kolkata")

# Fixed past trading day: Friday 2024-06-21 @ 11:00 IST; expiry next Thursday.
NOW = datetime(2024, 6, 21, 11, 0, tzinfo=IST)
EXPIRY = date(2024, 6, 27)  # dte from 2024-06-21 == 6 ... we inject dte explicitly
SPOT = 23_400.0


# ---------------------------------------------------------------------------
# Chain-row factory — mirrors parse_option_chain dict schema exactly.
# ---------------------------------------------------------------------------
def _row(
    strike: float,
    option_type: str,
    *,
    ltp: float = 120.0,
    oi: int = 500_000,
    volume: int = 50_000,
    bid: float | None = 119.0,
    ask: float | None = 121.0,
    bid_qty: int | None = 10_000,
    ask_qty: int | None = 10_000,
    iv: float | None = 14.0,
    delta: float | None = None,
    security_id: str | None = None,
) -> dict:
    return {
        "snapshot_time": NOW.astimezone(ZoneInfo("UTC")),
        "underlying_scrip": 13,
        "underlying_seg": "IDX_I",
        "expiry_date": EXPIRY,
        "strike": strike,
        "option_type": option_type,
        "security_id": security_id or f"{int(strike)}{option_type}",
        "ltp": ltp,
        "prev_close": ltp,
        "volume": volume,
        "oi": oi,
        "prev_oi": oi,
        "prev_volume": volume,
        "top_bid_price": bid,
        "top_ask_price": ask,
        "top_bid_qty": bid_qty,
        "top_ask_qty": ask_qty,
        "iv": iv,
        "delta": delta,
        "theta": -5.0,
        "gamma": 0.001,
        "vega": 8.0,
        "spot": SPOT,
        "raw": {},
    }


def _liquid_chain() -> list[dict]:
    """A healthy CE+PE chain around ATM 23400, step 50, all gates passable."""
    rows = []
    for k in range(23_200, 23_601, 50):
        # premium higher near/ITM; keep within band (max 400).
        ce_ltp = max(10.0, 250.0 - (k - 23_200) * 0.5)
        pe_ltp = max(10.0, 60.0 + (k - 23_200) * 0.4)
        rows.append(_row(k, "CE", ltp=ce_ltp, bid=ce_ltp - 0.5, ask=ce_ltp + 0.5))
        rows.append(_row(k, "PE", ltp=pe_ltp, bid=pe_ltp - 0.5, ask=pe_ltp + 0.5))
    return rows


def _inputs(chain, *, direction="LONG", dte=2, iv_hist=None) -> ScreenerInputs:
    return ScreenerInputs(
        index="NIFTY",
        direction=direction,
        spot=SPOT,
        chain_rows=chain,
        expiry_date=EXPIRY,
        dte=dte,
        now=NOW,
        iv_hist=iv_hist,
    )


# ---------------------------------------------------------------------------
# 1. ATM pick — LONG → CE at the ATM strike.
# ---------------------------------------------------------------------------
def test_atm_pick_long_ce():
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(_liquid_chain(), direction="LONG"))
    assert isinstance(out, ChosenContract)
    assert out.option_type == "CE"
    assert out.strike == 23_400.0  # ATM
    assert out.index == "NIFTY"
    assert out.ref_ltp > 0
    assert 0.0 <= out.score <= 1.0
    assert out.est_spread_pct is not None  # quotes present in forward-paper rows
    assert out.expiry_restricted is False  # dte=2 is in preferred 1..2


def test_short_picks_pe():
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(_liquid_chain(), direction="SHORT"))
    assert out is not None and out.option_type == "PE"
    assert out.strike == 23_400.0


# ---------------------------------------------------------------------------
# 2. Liquidity rejects — OI / volume below floor.
# ---------------------------------------------------------------------------
def test_oi_reject_all():
    chain = [_row(k, "CE", oi=10) for k in range(23_300, 23_501, 50)]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


def test_volume_reject_all():
    chain = [_row(k, "CE", volume=5) for k in range(23_300, 23_501, 50)]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


# ---------------------------------------------------------------------------
# 3. Spread reject — width too wide (% gate).
# ---------------------------------------------------------------------------
def test_spread_pct_reject_all():
    # mid ~120, width 20 => 16% spread >> 2%.
    chain = [
        _row(k, "CE", ltp=120.0, bid=110.0, ask=130.0)
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


def test_spread_abs_reject():
    # within 2% pct but over abs cap (NIFTY abs cap 3.0): mid 500, width 6 => 1.2% but >3 abs.
    chain = [
        _row(k, "CE", ltp=400.0, bid=397.0, ask=403.0)  # width 6 > 3 abs
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


# ---------------------------------------------------------------------------
# 4. Depth reject — L1 size below min lots.
# ---------------------------------------------------------------------------
def test_depth_reject_all():
    # NIFTY lot 75, min 5 lots => 375 units; give 100.
    chain = [
        _row(k, "CE", bid_qty=100, ask_qty=100)
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


# ---------------------------------------------------------------------------
# 5. Premium-band rejects.
# ---------------------------------------------------------------------------
def test_premium_too_cheap_reject():
    chain = [
        _row(k, "CE", ltp=2.0, bid=1.8, ask=2.2)  # < min 5
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


def test_premium_too_rich_reject():
    chain = [
        _row(k, "CE", ltp=900.0, bid=899.0, ask=901.0)  # > NIFTY max 400
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


# ---------------------------------------------------------------------------
# 6. Ranking tie → nearest ATM wins.
# ---------------------------------------------------------------------------
def test_tie_breaks_to_nearest_atm():
    # Two strikes equidistant in liquidity/spread; the nearer-ATM must win.
    # ATM 23400; offer 23400 (dist 0) and 23500 (dist 2 steps), identical liquidity.
    chain = [
        _row(23_400, "CE", ltp=120.0, oi=500_000, volume=50_000, bid=119.5, ask=120.5),
        _row(23_500, "CE", ltp=120.0, oi=500_000, volume=50_000, bid=119.5, ask=120.5),
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY", n_candidates=5))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None
    assert out.strike == 23_400.0  # nearest ATM


# ---------------------------------------------------------------------------
# 7. None when all fail (empty / wrong side).
# ---------------------------------------------------------------------------
def test_none_when_chain_empty():
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs([], direction="LONG"))
    assert out is None


def test_none_when_only_wrong_side():
    chain = [_row(k, "PE") for k in range(23_300, 23_501, 50)]  # only PE
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))  # wants CE
    assert out is None


# ---------------------------------------------------------------------------
# 8. DTE gate.
# ---------------------------------------------------------------------------
def test_dte_out_of_range_rejects():
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(_liquid_chain(), direction="LONG", dte=9))
    assert out is None


def test_expiry_restricted_flag_when_dte0():
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(_liquid_chain(), direction="LONG", dte=0))
    assert out is not None
    assert out.expiry_restricted is True  # 0 DTE outside preferred 1..2
    assert out.dte == 0


# ---------------------------------------------------------------------------
# 9. fail-open iv_rank — no history must NOT reject.
# ---------------------------------------------------------------------------
def test_iv_rank_fail_open_no_history():
    p = ScreenerParams(index="NIFTY", iv_rank_min=0.20, iv_rank_max=0.80)
    s = ScalperScreener(p)
    out = s.select(_inputs(_liquid_chain(), direction="LONG", iv_hist=None))
    assert out is not None  # no history => band gate fails OPEN


def test_iv_rank_fail_open_short_history():
    p = ScreenerParams(index="NIFTY", iv_rank_min=0.20, iv_rank_max=0.80)
    s = ScalperScreener(p)
    out = s.select(_inputs(_liquid_chain(), direction="LONG", iv_hist=[12.0]))
    assert out is not None  # < 2 points => fail open


def test_iv_rank_band_rejects_when_history_present():
    # today's median IV is 14; history all far below => iv_rank == 1.0 > max 0.80.
    hist = [5.0] * 60
    p = ScreenerParams(index="NIFTY", iv_rank_min=0.0, iv_rank_max=0.80)
    s = ScalperScreener(p)
    out = s.select(_inputs(_liquid_chain(), direction="LONG", iv_hist=hist))
    assert out is None  # iv_rank too high


# ---------------------------------------------------------------------------
# 10. Backtest projection path — bid/ask=None => spread/depth SKIPPED, still picks.
# ---------------------------------------------------------------------------
def test_backtest_rows_no_quotes_still_selects():
    chain = [
        _row(k, "CE", bid=None, ask=None, bid_qty=None, ask_qty=None)
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None
    assert out.est_spread_pct is None  # uncomputable in backtest
    assert "skipped" in out.reason
    assert out.strike == 23_400.0  # still nearest ATM by remaining sub-scores


def test_backtest_wide_spread_not_rejected_when_no_quotes():
    # Even a (notional) wide market is fine in backtest because there are no quotes.
    chain = [_row(k, "CE", bid=None, ask=None) for k in range(23_300, 23_501, 50)]
    s = ScalperScreener(ScreenerParams(index="NIFTY", max_spread_pct=0.0001))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None  # spread gate cannot run


# ---------------------------------------------------------------------------
# 11. strike_mode variants.
# ---------------------------------------------------------------------------
def test_offset_mode_picks_otm():
    p = ScreenerParams(index="NIFTY", strike_mode="offset", offset_steps=2)
    s = ScalperScreener(p)
    out = s.select(_inputs(_liquid_chain(), direction="LONG"))
    assert out is not None
    assert out.strike == 23_500.0  # ATM 23400 + 2*50


def test_delta_mode_targets_delta():
    chain = [
        _row(23_350, "CE", delta=0.62, ltp=140.0, bid=139.5, ask=140.5),
        _row(23_400, "CE", delta=0.50, ltp=120.0, bid=119.5, ask=120.5),
        _row(23_450, "CE", delta=0.38, ltp=95.0, bid=94.5, ask=95.5),
    ]
    p = ScreenerParams(index="NIFTY", strike_mode="delta", target_delta=0.50)
    s = ScalperScreener(p)
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None
    assert out.strike == 23_400.0  # |delta-0.50| minimal at the 0.50 row


# ---------------------------------------------------------------------------
# 12. BANKNIFTY index-agnostic — step 100, lot 35, richer premium band.
# ---------------------------------------------------------------------------
def test_banknifty_index_agnostic_atm():
    spot = 48_240.0  # ATM 48200 at step 100
    chain = []
    for k in range(48_000, 48_501, 100):
        chain.append(
            _row(k, "CE", ltp=300.0, oi=300_000, volume=40_000,
                 bid=299.0, ask=301.0, bid_qty=5_000, ask_qty=5_000, security_id=f"BN{k}")
        )
    inp = ScreenerInputs(
        index="BANKNIFTY", direction="LONG", spot=spot, chain_rows=chain,
        expiry_date=EXPIRY, dte=2, now=NOW,
    )
    s = ScalperScreener(ScreenerParams(index="BANKNIFTY"))
    out = s.select(inp)
    assert out is not None
    assert out.index == "BANKNIFTY"
    assert out.strike == 48_200.0  # ATM at step 100


# ---------------------------------------------------------------------------
# 13. Input validation.
# ---------------------------------------------------------------------------
def test_bad_direction_raises():
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    with pytest.raises(ValueError):
        s.select(_inputs(_liquid_chain(), direction="SIDEWAYS"))


def test_bad_strike_mode_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", strike_mode="weird")


def test_params_per_index_defaults():
    p = ScreenerParams(index="BANKNIFTY")
    assert p.step == 100
    assert p.lot == 35
    assert p.max_premium == 1200.0
    assert p.max_spread_abs == 8.0
    n = ScreenerParams(index="NIFTY")
    assert n.step == 50 and n.lot == 65 and n.max_premium == 400.0  # lot == fno_costs.NIFTY_LOT


def test_unknown_index_falls_back_to_nifty_grid():
    p = ScreenerParams(index="MIDCPNIFTY")
    assert p.step == 50 and p.lot == 65  # NIFTY fallback


# ---------------------------------------------------------------------------
# 14. Crossed book rejected (forward-paper sanity).
# ---------------------------------------------------------------------------
def test_crossed_book_rejected():
    chain = [
        _row(k, "CE", bid=125.0, ask=120.0)  # ask < bid
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


# ---------------------------------------------------------------------------
# 15. Score deterministic + bounded; nearer ATM scores >= farther (same liquidity).
# ---------------------------------------------------------------------------
def test_proximity_orders_score():
    chain = [
        _row(23_400, "CE", ltp=120.0, bid=119.5, ask=120.5),
        _row(23_550, "CE", ltp=80.0, bid=79.5, ask=80.5),
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None and out.strike == 23_400.0


# ---------------------------------------------------------------------------
# 16. offset mode is SIDE-AWARE: +offset is OTM (lower strike) for PE.
# ---------------------------------------------------------------------------
def test_offset_mode_pe_otm_is_lower_strike():
    # ATM 23400; SHORT→PE; offset 2 OTM ⇒ 23400 - 2*50 = 23300.
    chain = []
    for k in range(23_200, 23_601, 50):
        chain.append(_row(k, "PE", ltp=100.0, bid=99.5, ask=100.5))
    p = ScreenerParams(index="NIFTY", strike_mode="offset", offset_steps=2)
    s = ScalperScreener(p)
    out = s.select(_inputs(chain, direction="SHORT"))
    assert out is not None
    assert out.strike == 23_300.0  # PE OTM is BELOW ATM


def test_offset_mode_ce_otm_is_higher_strike():
    chain = []
    for k in range(23_200, 23_601, 50):
        chain.append(_row(k, "CE", ltp=100.0, bid=99.5, ask=100.5))
    p = ScreenerParams(index="NIFTY", strike_mode="offset", offset_steps=2)
    s = ScalperScreener(p)
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None
    assert out.strike == 23_500.0  # CE OTM is ABOVE ATM


# ---------------------------------------------------------------------------
# 17. security_id=None must NEVER be returned (cannot route an order).
# ---------------------------------------------------------------------------
def test_missing_security_id_rejected():
    chain = [
        _row(k, "CE", security_id=None, bid=None, ask=None)  # backtest-ish, but no id
        for k in range(23_300, 23_501, 50)
    ]
    # force security_id None explicitly (factory default would synthesize one)
    for r in chain:
        r["security_id"] = None
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is None


# ---------------------------------------------------------------------------
# 18. param validation raises on misconfiguration.
# ---------------------------------------------------------------------------
def test_step_zero_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", step=0)


def test_premium_band_inverted_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", min_premium=500.0, max_premium=400.0)


def test_dte_inverted_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", dte_min=4, dte_max=0)


def test_spread_pct_out_of_range_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", max_spread_pct=2.0)


def test_target_delta_out_of_range_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", strike_mode="delta", target_delta=1.5)


def test_iv_rank_band_inverted_raises():
    with pytest.raises(ValueError):
        ScreenerParams(index="NIFTY", iv_rank_min=0.8, iv_rank_max=0.2)


# ---------------------------------------------------------------------------
# 19. one-sided book (bid present, ask None) → gates skipped, still selects.
# ---------------------------------------------------------------------------
def test_one_sided_book_skips_gates(caplog):
    chain = [
        _row(k, "CE", bid=119.0, ask=None, bid_qty=10_000, ask_qty=None)
        for k in range(23_300, 23_501, 50)
    ]
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out = s.select(_inputs(chain, direction="LONG"))
    assert out is not None  # cannot trust spread → skip, not hard-fail
    assert out.est_spread_pct is None


# ---------------------------------------------------------------------------
# 20. determinism: shuffled chain_rows yield identical pick.
# ---------------------------------------------------------------------------
def test_deterministic_under_row_order():
    base = _liquid_chain()
    s = ScalperScreener(ScreenerParams(index="NIFTY"))
    out1 = s.select(_inputs(list(base), direction="LONG"))
    out2 = s.select(_inputs(list(reversed(base)), direction="LONG"))
    assert out1 is not None and out2 is not None
    assert out1.strike == out2.strike and out1.security_id == out2.security_id
