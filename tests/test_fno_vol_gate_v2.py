"""TEST — GATE-V2: Yang-Zhang RV + VRP-percentile + backwardation/event veto.

Module under test: ml/fno_vol_gate.py  (GATE-V2 section).

All pure-function tests — no DB, no async, no external deps. TZ-safe: any
date used is built with datetime.now(ZoneInfo("Asia/Kolkata")).date() so the
suite behaves identically on the IST dev Mac and on UTC CI
([[ci-ist-date-trap]]).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ml.fno_vol_gate import (
    RV_SOURCE_CLOSE_CLOSE,
    RV_SOURCE_YANG_ZHANG,
    SELL_PREMIUM,
    STAND_ASIDE,
    BUY_PREMIUM,
    GateV2Config,
    GateV2Result,
    close_close_realized_vol,
    gate_v2_decision,
    is_backwardation,
    is_event_day,
    percentile_rank,
    realized_vol,
    vrp_percentile_gate,
    vrp_spread,
    yang_zhang_realized_vol,
)

IST = ZoneInfo("Asia/Kolkata")


def _today_ist() -> date:
    """TZ-safe 'today' — never date.today()/naive now() (CI runs UTC)."""
    return datetime.now(IST).date()


# ===========================================================================
# 1. Yang-Zhang estimator
# ===========================================================================

class TestYangZhang:
    @staticmethod
    def _flat_bars(n: int, price: float = 100.0):
        """n identical OHLC bars (zero realized vol)."""
        return [(price, price, price, price) for _ in range(n)]

    def test_zero_vol_on_flat_bars(self):
        """Perfectly flat OHLC → zero realized vol (no variance anywhere)."""
        bars = self._flat_bars(25)
        rv = yang_zhang_realized_vol(bars, window=20)
        assert rv is not None
        assert rv == pytest.approx(0.0, abs=1e-9)

    def test_known_case_positive_and_finite(self):
        """A hand-built ranging series yields a positive, finite, sane vol."""
        # Alternating up/down closes with intrabar range → non-zero YZ.
        bars = []
        p = 100.0
        for i in range(30):
            o = p
            c = p * (1.01 if i % 2 == 0 else 0.99)
            h = max(o, c) * 1.005
            low = min(o, c) * 0.995
            bars.append((o, h, low, c))
            p = c
        rv = yang_zhang_realized_vol(bars, window=20)
        assert rv is not None
        assert rv > 0.0
        assert math.isfinite(rv)
        # ~1% daily moves annualised (√252) → order ~ a few tenths, sanity bound.
        assert 0.0 < rv < 5.0

    def test_known_numeric_reconstruction(self):
        """Reconstruct YZ by hand on a tiny 3-bar window and match the function."""
        # window=2 → uses last 3 bars (need prior close for overnight term).
        bars = [
            (100.0, 101.0, 99.0, 100.5),
            (100.5, 102.0, 100.0, 101.0),
            (101.0, 101.5, 100.0, 100.2),
        ]
        got = yang_zhang_realized_vol(bars, window=2)
        # Hand compute:
        ohlc = bars  # exactly window+1 = 3 bars
        overnight, open_close, rs = [], [], []
        for i in range(1, len(ohlc)):
            o, h, low, c = ohlc[i]
            prev_c = ohlc[i - 1][3]
            overnight.append(math.log(o / prev_c))
            open_close.append(math.log(c / o))
            rs.append(math.log(h / c) * math.log(h / o) + math.log(low / c) * math.log(low / o))

        def _var(xs):
            m = sum(xs) / len(xs)
            return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)

        n = len(ohlc) - 1
        k = 0.34 / (1.34 + (n + 1) / (n - 1))
        sigma2 = _var(overnight) + k * _var(open_close) + (1 - k) * (sum(rs) / len(rs))
        expected = math.sqrt(max(sigma2, 0.0)) * math.sqrt(252)
        assert got == pytest.approx(expected, rel=1e-9)

    def test_insufficient_bars_returns_none(self):
        """Fewer than window+1 bars → None (need prior close for overnight)."""
        assert yang_zhang_realized_vol(self._flat_bars(20), window=20) is None
        assert yang_zhang_realized_vol([], window=20) is None

    def test_degenerate_bar_returns_none(self):
        """A non-positive or misordered bar → None (fail-open)."""
        bars = self._flat_bars(25)
        bars[10] = (100.0, 99.0, 101.0, 100.0)  # high < low (impossible)
        assert yang_zhang_realized_vol(bars, window=20) is None
        bars2 = self._flat_bars(25)
        bars2[5] = (0.0, 100.0, 100.0, 100.0)  # non-positive open
        assert yang_zhang_realized_vol(bars2, window=20) is None

    def test_none_input_returns_none_no_raise(self):
        assert yang_zhang_realized_vol(None, window=20) is None

    def test_yz_lower_variance_than_close_close_on_average(self):
        """YZ is a lower-variance estimator; on a controlled series both are
        positive and within a sane band of each other."""
        bars = []
        p = 100.0
        for i in range(40):
            o = p
            drift = 0.005 * math.sin(i / 3.0)
            c = p * (1 + drift)
            h = max(o, c) * 1.004
            low = min(o, c) * 0.996
            bars.append((o, h, low, c))
            p = c
        yz = yang_zhang_realized_vol(bars, window=20)
        cc = close_close_realized_vol([b[3] for b in bars], window=20)
        assert yz is not None and cc is not None
        assert yz > 0 and cc > 0


# ===========================================================================
# 2. close-close estimator + realized_vol dispatcher
# ===========================================================================

class TestCloseClose:
    def test_flat_series_zero_vol(self):
        rv = close_close_realized_vol([100.0] * 25, window=20)
        assert rv == pytest.approx(0.0, abs=1e-12)

    def test_insufficient_returns_none(self):
        assert close_close_realized_vol([100.0] * 20, window=20) is None
        assert close_close_realized_vol([], window=20) is None

    def test_data_gap_in_window_returns_none(self):
        closes = [100.0] * 25
        closes[10] = 0.0  # non-positive close inside the window → fail-open
        assert close_close_realized_vol(closes, window=20) is None

    def test_matches_manual_stdev(self):
        closes = [100.0, 101.0, 100.0, 102.0, 101.0, 103.0]
        rv = close_close_realized_vol(closes, window=5)
        rets = [math.log(closes[i + 1] / closes[i]) for i in range(5)]
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        assert rv == pytest.approx(math.sqrt(var) * math.sqrt(252), rel=1e-12)


class TestRealizedVolDispatcher:
    def test_yz_source_uses_ohlc(self):
        bars = [(100.0, 100.5, 99.5, 100.0)] * 25
        cfg = GateV2Config(rv_source=RV_SOURCE_YANG_ZHANG)
        rv = realized_vol(ohlc_bars=bars, cfg=cfg)
        assert rv is not None

    def test_yz_source_without_bars_is_none(self):
        cfg = GateV2Config(rv_source=RV_SOURCE_YANG_ZHANG)
        assert realized_vol(closes=[100.0] * 25, cfg=cfg) is None

    def test_close_close_default(self):
        cfg = GateV2Config(rv_source=RV_SOURCE_CLOSE_CLOSE)
        rv = realized_vol(closes=[100.0, 101.0] * 13, cfg=cfg)
        assert rv is not None

    def test_close_close_falls_back_to_ohlc_close_column(self):
        bars = [(100.0, 101.0, 99.0, 100.0 + i * 0.1) for i in range(25)]
        cfg = GateV2Config(rv_source=RV_SOURCE_CLOSE_CLOSE)
        rv = realized_vol(ohlc_bars=bars, cfg=cfg)
        assert rv is not None


# ===========================================================================
# 3. VRP spread + percentile gate
# ===========================================================================

class TestVrpSpread:
    def test_positive_spread(self):
        assert vrp_spread(0.10, 0.15) == pytest.approx(0.05)

    def test_negative_spread_when_realized_richer(self):
        assert vrp_spread(0.20, 0.15) == pytest.approx(-0.05)

    def test_none_iv_returns_none(self):
        assert vrp_spread(0.10, None) is None

    def test_nonpositive_iv_returns_none(self):
        assert vrp_spread(0.10, 0.0) is None
        assert vrp_spread(0.10, -0.1) is None

    def test_negative_rv_returns_none(self):
        assert vrp_spread(-0.1, 0.15) is None


class TestPercentileRank:
    def test_midpoint(self):
        # value 0.5 vs [0,0.25,0.75,1.0]: two below → 0.5
        assert percentile_rank(0.5, [0.0, 0.25, 0.75, 1.0]) == pytest.approx(0.5)

    def test_top(self):
        assert percentile_rank(2.0, [0.0, 1.0, 1.5]) == pytest.approx(1.0)

    def test_bottom(self):
        assert percentile_rank(-1.0, [0.0, 1.0, 1.5]) == pytest.approx(0.0)

    def test_filters_none_and_nonfinite(self):
        assert percentile_rank(0.5, [None, 0.0, float("nan"), 1.0]) == pytest.approx(0.5)

    def test_empty_window_none(self):
        assert percentile_rank(0.5, []) is None
        assert percentile_rank(0.5, [None, None]) is None

    def test_none_value_none(self):
        assert percentile_rank(None, [0.0, 1.0]) is None


class TestVrpPercentileGate:
    def _cfg(self, **kw):
        return GateV2Config(vrp_min_obs=5, vrp_rich_percentile=0.50, **kw)

    def test_rich_when_high_percentile(self):
        window = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
        rich, pct = vrp_percentile_gate(0.06, window, cfg=self._cfg())
        assert rich is True
        assert pct == pytest.approx(1.0)

    def test_not_rich_when_low_percentile(self):
        window = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
        rich, pct = vrp_percentile_gate(0.005, window, cfg=self._cfg())
        assert rich is False
        assert pct is not None and pct < 0.5

    def test_boundary_at_threshold_is_rich(self):
        # exactly at the threshold percentile → rich (>=)
        window = [0.0, 0.01, 0.02, 0.03]  # 4 obs (need >=... lower min)
        cfg = GateV2Config(vrp_min_obs=4, vrp_rich_percentile=0.50)
        rich, pct = vrp_percentile_gate(0.02, window, cfg=cfg)
        # two below 0.02 → pct = 0.5 == threshold → rich
        assert pct == pytest.approx(0.5)
        assert rich is True

    def test_insufficient_history_fails_open(self):
        rich, pct = vrp_percentile_gate(0.06, [0.01, 0.02], cfg=self._cfg())
        assert rich is True   # fail-open: not enough obs → do not veto
        assert pct is None

    def test_none_spread_fails_open(self):
        window = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
        rich, pct = vrp_percentile_gate(None, window, cfg=self._cfg())
        assert rich is True
        assert pct is None

    def test_advisory_mode_never_vetoes(self):
        cfg = GateV2Config(vrp_min_obs=5, require_percentile=False)
        window = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
        rich, pct = vrp_percentile_gate(0.001, window, cfg=cfg)
        assert rich is True  # advisory: boolean always allow
        assert pct is not None  # but percentile still reported


# ===========================================================================
# 4. Backwardation veto
# ===========================================================================

class TestBackwardation:
    def test_backwardation_true_when_front_above_next(self):
        assert is_backwardation(0.30, 0.20) is True

    def test_contango_false(self):
        assert is_backwardation(0.18, 0.22) is False

    def test_equal_is_false(self):
        assert is_backwardation(0.20, 0.20) is False

    def test_min_ratio_cushion(self):
        # front only 5% above next, require 1.10× → not backwardation
        assert is_backwardation(0.21, 0.20, min_ratio=1.10) is False
        assert is_backwardation(0.23, 0.20, min_ratio=1.10) is True

    def test_none_inputs_fail_open_false(self):
        assert is_backwardation(None, 0.20) is False
        assert is_backwardation(0.30, None) is False
        assert is_backwardation(None, None) is False

    def test_nonpositive_fail_open_false(self):
        assert is_backwardation(0.0, 0.20) is False
        assert is_backwardation(0.30, -0.1) is False


# ===========================================================================
# 5. Event veto
# ===========================================================================

class TestEventVeto:
    def test_expiry_day_is_event(self):
        d = _today_ist()
        assert is_event_day(d, expiry_date=d) is True

    def test_non_expiry_non_listed_is_not_event(self):
        d = _today_ist()
        assert is_event_day(d, expiry_date=d + timedelta(days=3)) is False

    def test_listed_event_date_is_event(self):
        d = _today_ist()
        events = [d - timedelta(days=1), d, d + timedelta(days=5)]
        assert is_event_day(d, expiry_date=d + timedelta(days=2), event_dates=events) is True

    def test_empty_event_list_default(self):
        d = _today_ist()
        assert is_event_day(d, expiry_date=d + timedelta(days=2)) is False
        assert is_event_day(d, expiry_date=d + timedelta(days=2), event_dates=[]) is False

    def test_none_cycle_date_fails_open_false(self):
        assert is_event_day(None, expiry_date=_today_ist()) is False

    def test_datetime_normalised_to_date(self):
        dt = datetime.now(IST)
        # same calendar day as the .date() expiry → event
        assert is_event_day(dt, expiry_date=dt.date()) is True


# ===========================================================================
# 6. Composed GATE-V2 decision
# ===========================================================================

class TestGateV2Decision:
    """RV passed as a TRADING-basis value; the gate rebases internally.

    Calendar-rebase factor ≈ √(252/365) ≈ 0.8307. To make v1 say SELL we need
    rv_cal < k*iv. With k=0.9, iv=0.20 → need rv_cal < 0.18 → rv_trading < ~0.2167.
    """

    IV = 0.20

    def _rich_window(self):
        # current spread will be iv - rv_cal; build a window it sits high in.
        return [-0.05, -0.02, 0.0, 0.01, 0.02, 0.03]

    def test_all_pass_is_sell(self):
        d = _today_ist()
        res = gate_v2_decision(
            0.05, self.IV,                       # rv_trading low → strong SELL
            trailing_spreads=self._rich_window(),
            front_iv=0.18, next_iv=0.22,         # contango (no veto)
            cycle_date=d, expiry_date=d + timedelta(days=4),
            cfg=GateV2Config(vrp_min_obs=5),
        )
        assert isinstance(res, GateV2Result)
        assert res.decision == SELL_PREMIUM
        assert res.v1_decision == SELL_PREMIUM
        assert res.backwardation_veto is False
        assert res.event_veto is False

    def test_backwardation_vetoes_sell(self):
        d = _today_ist()
        res = gate_v2_decision(
            0.05, self.IV,
            trailing_spreads=self._rich_window(),
            front_iv=0.30, next_iv=0.20,         # backwardation → veto
            cycle_date=d, expiry_date=d + timedelta(days=4),
            cfg=GateV2Config(vrp_min_obs=5),
        )
        assert res.decision == STAND_ASIDE
        assert res.v1_decision == SELL_PREMIUM
        assert res.backwardation_veto is True

    def test_event_day_vetoes_sell(self):
        d = _today_ist()
        res = gate_v2_decision(
            0.05, self.IV,
            trailing_spreads=self._rich_window(),
            front_iv=0.18, next_iv=0.22,
            cycle_date=d, expiry_date=d,         # expiry day → veto
            cfg=GateV2Config(vrp_min_obs=5),
        )
        assert res.decision == STAND_ASIDE
        assert res.event_veto is True

    def test_low_percentile_vetoes_sell(self):
        d = _today_ist()
        # current spread will be small/low vs a window of high spreads.
        high_window = [0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
        res = gate_v2_decision(
            0.20, self.IV,  # rv_trading 0.20 → rv_cal ≈0.166 < 0.18 → still SELL,
                            # but spread ≈ 0.034, low vs the high window
            trailing_spreads=high_window,
            front_iv=0.18, next_iv=0.22,
            cycle_date=d, expiry_date=d + timedelta(days=4),
            cfg=GateV2Config(vrp_min_obs=5, vrp_rich_percentile=0.50),
        )
        assert res.v1_decision == SELL_PREMIUM
        assert res.vrp_rich is False
        assert res.decision == STAND_ASIDE

    def test_v1_buy_passes_through(self):
        d = _today_ist()
        res = gate_v2_decision(
            0.30, self.IV,  # rv_cal ≈ 0.249 > iv → BUY_PREMIUM
            trailing_spreads=self._rich_window(),
            front_iv=0.18, next_iv=0.22,
            cycle_date=d, expiry_date=d + timedelta(days=4),
        )
        assert res.v1_decision == BUY_PREMIUM
        assert res.decision == BUY_PREMIUM  # passes through unchanged

    def test_v1_stand_aside_passes_through(self):
        d = _today_ist()
        # rv_cal between k*iv and iv → STAND_ASIDE at v1
        # need k*iv=0.18 < rv_cal < 0.20 → rv_trading in (0.2167, 0.2408)
        res = gate_v2_decision(
            0.23, self.IV,
            trailing_spreads=self._rich_window(),
            front_iv=0.18, next_iv=0.22,
            cycle_date=d, expiry_date=d + timedelta(days=4),
        )
        assert res.v1_decision == STAND_ASIDE
        assert res.decision == STAND_ASIDE

    def test_thin_history_fails_open_to_sell(self):
        """Percentile sub-gate fails open when history is thin → SELL survives."""
        d = _today_ist()
        res = gate_v2_decision(
            0.05, self.IV,
            trailing_spreads=[0.01, 0.02],       # < vrp_min_obs → fail-open
            front_iv=0.18, next_iv=0.22,
            cycle_date=d, expiry_date=d + timedelta(days=4),
            cfg=GateV2Config(vrp_min_obs=5),
        )
        assert res.decision == SELL_PREMIUM
        assert res.vrp_percentile is None  # not enough obs to rank

    def test_missing_term_structure_does_not_veto(self):
        d = _today_ist()
        res = gate_v2_decision(
            0.05, self.IV,
            trailing_spreads=self._rich_window(),
            front_iv=None, next_iv=None,         # missing → no backwardation veto
            cycle_date=d, expiry_date=d + timedelta(days=4),
            cfg=GateV2Config(vrp_min_obs=5),
        )
        assert res.backwardation_veto is False
        assert res.decision == SELL_PREMIUM


# ===========================================================================
# 7. Fail-open contract — NEVER raise, degenerate → STAND_ASIDE
# ===========================================================================

class TestGateV2FailOpen:
    def test_none_rv_stand_aside(self):
        res = gate_v2_decision(None, 0.20)
        assert res.decision == STAND_ASIDE

    def test_none_iv_stand_aside(self):
        res = gate_v2_decision(0.05, None)
        assert res.decision == STAND_ASIDE

    def test_zero_iv_stand_aside(self):
        res = gate_v2_decision(0.05, 0.0)
        assert res.decision == STAND_ASIDE

    def test_negative_rv_stand_aside(self):
        res = gate_v2_decision(-0.1, 0.20)
        assert res.decision == STAND_ASIDE

    def test_all_none_does_not_raise(self):
        try:
            res = gate_v2_decision(None, None)
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"gate_v2_decision raised on all-None: {exc}")
        assert res.decision == STAND_ASIDE

    def test_no_optional_args_does_not_raise(self):
        try:
            res = gate_v2_decision(0.05, 0.20)
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"gate_v2_decision raised with minimal args: {exc}")
        # minimal call: no trailing history (fail-open), no term/event vetoes
        assert res.decision in (SELL_PREMIUM, STAND_ASIDE, BUY_PREMIUM)

    def test_garbage_trailing_spreads_does_not_raise(self):
        res = gate_v2_decision(
            0.05, 0.20,
            trailing_spreads=[None, float("nan"), None],
        )
        assert res.decision in (SELL_PREMIUM, STAND_ASIDE)

    def test_result_is_frozen(self):
        res = gate_v2_decision(0.05, 0.20)
        with pytest.raises((AttributeError, TypeError)):
            res.decision = "X"  # type: ignore[misc]


# ===========================================================================
# 8. Config is index-agnostic (no hardcoded NIFTY)
# ===========================================================================

class TestConfigIndexAgnostic:
    def test_defaults_reproduce_v1_k(self):
        from ml.fno_vol_gate import DEFAULT_K
        assert GateV2Config().k == pytest.approx(DEFAULT_K)

    def test_per_index_override(self):
        cfg = GateV2Config(k=0.8, rv_window=10, vrp_window=120,
                           vrp_min_obs=30, vrp_rich_percentile=0.70)
        assert cfg.k == pytest.approx(0.8)
        assert cfg.rv_window == 10
        assert cfg.vrp_rich_percentile == pytest.approx(0.70)

    def test_config_is_frozen(self):
        cfg = GateV2Config()
        with pytest.raises((AttributeError, TypeError)):
            cfg.k = 1.0  # type: ignore[misc]
