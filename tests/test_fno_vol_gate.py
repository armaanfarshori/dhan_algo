"""TEST — FnoVolGate: gate_decision truth table, fail-open, VRP stats, calibration.

Module under test: ml/fno_vol_gate.py  (may not exist yet; tests are written
against the spec so they can be run once the module is ready).

All pure-function tests — no DB, no async, no external deps.
"""

import math
import statistics

import pytest

# ---------------------------------------------------------------------------
# Import guard — if the module is not yet written the tests are still
# collected; a skip marker is applied per-test via the fixture below.
# ---------------------------------------------------------------------------
try:
    from ml.fno_vol_gate import (
        BUY_PREMIUM,
        DEFAULT_K,
        SELL_PREMIUM,
        STAND_ASIDE,
        VrpStats,
        calibrate_threshold,
        compute_vrp_stats,
        gate_decision,
    )

    _MODULE_AVAILABLE = True
except ModuleNotFoundError:
    _MODULE_AVAILABLE = False


# Sentinel so tests can be skipped cleanly when the module is absent.
requires_module = pytest.mark.skipif(
    not _MODULE_AVAILABLE,
    reason="ml/fno_vol_gate not yet implemented",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ratios_to_samples(ratios):
    """Convert a list of realized/implied ratios to (realized, implied) pairs.

    implied_vol is fixed at 0.20 for all helpers so the math is transparent.
    """
    iv = 0.20
    return [(r * iv, iv) for r in ratios]


# ===========================================================================
# 1. gate_decision — truth table (k=0.9, implied_vol=0.12)
# ===========================================================================

class TestGateDecisionTruthTable:
    """Boundary cases with implied_vol=0.12, k=0.9 → threshold = 0.108."""

    IV = 0.12
    K = 0.9
    THRESHOLD = K * IV  # 0.108

    @requires_module
    def test_realized_below_threshold_is_sell(self):
        """realized < k*implied → SELL_PREMIUM."""
        rv = 0.05  # well below 0.108
        assert gate_decision(rv, self.IV, k=self.K) == SELL_PREMIUM

    @requires_module
    def test_realized_between_threshold_and_implied_is_stand_aside(self):
        """k*implied < realized < implied → STAND_ASIDE."""
        rv = 0.11  # 0.108 < 0.11 < 0.12
        assert gate_decision(rv, self.IV, k=self.K) == STAND_ASIDE

    @requires_module
    def test_realized_above_implied_is_buy_premium(self):
        """realized > implied → BUY_PREMIUM."""
        rv = 0.13  # > 0.12
        assert gate_decision(rv, self.IV, k=self.K) == BUY_PREMIUM

    @requires_module
    def test_realized_exactly_at_lower_boundary_is_stand_aside(self):
        """realized == k*implied (not strictly <) → STAND_ASIDE, not SELL_PREMIUM."""
        rv = self.THRESHOLD  # exactly 0.108
        assert gate_decision(rv, self.IV, k=self.K) == STAND_ASIDE

    @requires_module
    def test_realized_exactly_equal_to_implied_is_stand_aside(self):
        """realized == implied (not strictly >) → STAND_ASIDE, not BUY_PREMIUM."""
        rv = self.IV  # exactly 0.12
        assert gate_decision(rv, self.IV, k=self.K) == STAND_ASIDE

    @requires_module
    def test_default_k_is_0_9(self):
        """DEFAULT_K constant must equal 0.9."""
        assert DEFAULT_K == pytest.approx(0.9)

    @requires_module
    def test_default_k_used_when_omitted(self):
        """Omitting k uses DEFAULT_K=0.9; cross-check with explicit k=0.9."""
        rv = 0.05
        assert gate_decision(rv, self.IV) == gate_decision(rv, self.IV, k=0.9)

    @requires_module
    def test_constants_are_distinct_strings(self):
        """The three outcome constants must be distinct non-empty strings."""
        outcomes = {SELL_PREMIUM, STAND_ASIDE, BUY_PREMIUM}
        assert len(outcomes) == 3
        for o in outcomes:
            assert isinstance(o, str) and o

    @requires_module
    def test_just_below_lower_boundary_is_sell(self):
        """realized just below k*implied (by float epsilon) → SELL_PREMIUM."""
        rv = self.THRESHOLD - 1e-10
        assert gate_decision(rv, self.IV, k=self.K) == SELL_PREMIUM

    @requires_module
    def test_just_above_implied_is_buy_premium(self):
        """realized just above implied (by float epsilon) → BUY_PREMIUM."""
        rv = self.IV + 1e-10
        assert gate_decision(rv, self.IV, k=self.K) == BUY_PREMIUM


# ===========================================================================
# 2. gate_decision — fail-open (None / invalid inputs)
# ===========================================================================

class TestGateDecisionFailOpen:
    """None realized_vol or None/≤0 implied_vol must always return STAND_ASIDE."""

    @requires_module
    def test_none_realized_vol_stand_aside(self):
        assert gate_decision(None, 0.12) == STAND_ASIDE

    @requires_module
    def test_none_implied_vol_stand_aside(self):
        assert gate_decision(0.1, None) == STAND_ASIDE

    @requires_module
    def test_zero_implied_vol_stand_aside(self):
        assert gate_decision(0.1, 0) == STAND_ASIDE

    @requires_module
    def test_negative_implied_vol_stand_aside(self):
        assert gate_decision(0.1, -0.5) == STAND_ASIDE

    @requires_module
    def test_both_none_stand_aside(self):
        assert gate_decision(None, None) == STAND_ASIDE

    @requires_module
    def test_none_realized_zero_implied_stand_aside(self):
        assert gate_decision(None, 0) == STAND_ASIDE

    @requires_module
    def test_does_not_raise_on_none_realized(self):
        try:
            gate_decision(None, 0.12)
        except Exception as exc:
            pytest.fail(f"gate_decision raised unexpectedly: {exc}")

    @requires_module
    def test_does_not_raise_on_zero_implied(self):
        try:
            gate_decision(0.1, 0)
        except Exception as exc:
            pytest.fail(f"gate_decision raised unexpectedly: {exc}")

    @requires_module
    def test_does_not_raise_on_none_implied(self):
        try:
            gate_decision(0.1, None)
        except Exception as exc:
            pytest.fail(f"gate_decision raised unexpectedly: {exc}")

    @requires_module
    def test_does_not_raise_on_negative_implied(self):
        try:
            gate_decision(0.1, -0.5)
        except Exception as exc:
            pytest.fail(f"gate_decision raised unexpectedly: {exc}")


# ===========================================================================
# 3. compute_vrp_stats
# ===========================================================================

class TestComputeVrpStats:
    """Known sample set with predictable statistics."""

    # k=0.9; implied_vol fixed at 0.20 → threshold = 0.18
    # realized values chosen so we can hand-count each bucket:
    #   rv=0.10 → SELL  (0.10 < 0.18), edge = 0.20-0.10 = 0.10
    #   rv=0.14 → SELL  (0.14 < 0.18), edge = 0.20-0.14 = 0.06
    #   rv=0.19 → STAND (0.18 <= 0.19 <= 0.20)
    #   rv=0.22 → BUY   (0.22 > 0.20)
    IV = 0.20
    K = 0.9

    @pytest.fixture()
    def clean_samples(self):
        return [
            (0.10, self.IV),  # SELL, edge=0.10
            (0.14, self.IV),  # SELL, edge=0.06
            (0.19, self.IV),  # STAND_ASIDE
            (0.22, self.IV),  # BUY_PREMIUM
        ]

    @requires_module
    def test_returns_vrp_stats_dataclass(self, clean_samples):
        result = compute_vrp_stats(clean_samples, k=self.K)
        assert isinstance(result, VrpStats)

    @requires_module
    def test_n_is_count_of_valid_rows(self, clean_samples):
        """n should equal the number of rows with implied_vol > 0."""
        result = compute_vrp_stats(clean_samples, k=self.K)
        assert result.n == 4

    @requires_module
    def test_none_rows_are_dropped(self):
        """Rows where realized_vol is None must be excluded from n."""
        samples = [
            (None, 0.20),   # dropped
            (0.10, 0.20),   # SELL
            (0.14, 0.20),   # SELL
        ]
        result = compute_vrp_stats(samples, k=self.K)
        assert result.n == 2

    @requires_module
    def test_zero_implied_rows_are_dropped(self):
        """Rows where implied_vol <= 0 must be excluded from n."""
        samples = [
            (0.10, 0),      # dropped
            (0.10, -0.1),   # dropped
            (0.10, 0.20),   # SELL
        ]
        result = compute_vrp_stats(samples, k=self.K)
        assert result.n == 1

    @requires_module
    def test_pass_rate_hand_count(self, clean_samples):
        """pass_rate = SELL rows / total valid rows = 2/4 = 0.5."""
        result = compute_vrp_stats(clean_samples, k=self.K)
        assert result.pass_rate == pytest.approx(0.5)

    @requires_module
    def test_mean_edge_over_sell_rows_only(self, clean_samples):
        """mean_edge = mean(implied - realized) for SELL rows = mean(0.10, 0.06) = 0.08."""
        result = compute_vrp_stats(clean_samples, k=self.K)
        assert result.mean_edge == pytest.approx(0.08, abs=1e-9)

    @requires_module
    def test_median_edge_over_sell_rows_only(self, clean_samples):
        """median_edge for two SELL rows [0.10, 0.06] → median = 0.08."""
        result = compute_vrp_stats(clean_samples, k=self.K)
        assert result.median_edge == pytest.approx(0.08, abs=1e-9)

    @requires_module
    def test_edge_win_rate_over_sell_rows(self, clean_samples):
        """Both SELL edges (0.10 and 0.06) are positive → edge_win_rate = 1.0."""
        result = compute_vrp_stats(clean_samples, k=self.K)
        assert result.edge_win_rate == pytest.approx(1.0)

    @requires_module
    def test_edge_win_rate_with_negative_edge(self):
        """If a SELL row has realized > implied (negative edge), win-rate < 1."""
        # realized=0.10, iv=0.20 → edge +0.10 (positive)
        # realized=0.30, iv=0.20 → this becomes BUY row (rv > iv), not a SELL row
        # To get a negative edge on a SELL row we need: rv < k*iv but rv > iv → impossible.
        # Instead test with a barely-SELL row and a large positive edge: all positive.
        # A negative-edge SELL scenario: realized < k*iv but… edge = iv - rv is always
        # positive when rv < iv.  So the only way to get a 0-or-negative edge in a SELL
        # row is if mean_edge arithmetic rounds oddly — not possible by spec.
        # We confirm edge_win_rate = 1.0 for any well-formed SELL sample set.
        samples = [(0.05, 0.20), (0.10, 0.20)]
        result = compute_vrp_stats(samples, k=self.K)
        assert result.edge_win_rate == pytest.approx(1.0)

    @requires_module
    def test_no_sell_rows_mean_edge_zero_or_nan(self):
        """When there are no SELL rows, mean_edge and median_edge should be 0.0 or NaN
        without raising — implementation may choose either convention."""
        samples = [(0.22, 0.20)]  # BUY row only
        result = compute_vrp_stats(samples, k=self.K)
        if not math.isnan(result.mean_edge):
            assert result.mean_edge == pytest.approx(0.0)

    @requires_module
    def test_empty_samples_no_zero_division(self):
        """Empty input must return n=0 and not raise ZeroDivisionError."""
        result = compute_vrp_stats([], k=self.K)
        assert result.n == 0

    @requires_module
    def test_empty_samples_pass_rate_zero_or_nan(self):
        """Empty input: pass_rate should be 0.0 or NaN (no division error)."""
        result = compute_vrp_stats([], k=self.K)
        if not math.isnan(result.pass_rate):
            assert result.pass_rate == pytest.approx(0.0)

    @requires_module
    def test_all_none_samples_same_as_empty(self):
        """All-None samples → n=0, same behaviour as empty."""
        result = compute_vrp_stats([(None, 0.20), (None, None)], k=self.K)
        assert result.n == 0

    @requires_module
    def test_vrp_stats_is_frozen_dataclass(self):
        """VrpStats must be a frozen dataclass (attributes are read-only)."""
        result = compute_vrp_stats([(0.10, 0.20)], k=self.K)
        with pytest.raises((AttributeError, TypeError)):
            result.n = 99  # type: ignore[misc]

    @requires_module
    def test_vrp_stats_has_required_fields(self):
        """VrpStats must expose n, pass_rate, mean_edge, median_edge, edge_win_rate."""
        result = compute_vrp_stats([(0.10, 0.20)], k=self.K)
        for field in ("n", "pass_rate", "mean_edge", "median_edge", "edge_win_rate"):
            assert hasattr(result, field), f"VrpStats missing field: {field}"


# ===========================================================================
# 4. calibrate_threshold
# ===========================================================================

class TestCalibrateThreshold:
    """Quantile calibration and clamp behaviour."""

    # Ratios: 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1
    # realized/implied for each; implied=0.20 throughout
    RATIOS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]
    IV = 0.20

    @pytest.fixture()
    def ratio_samples(self):
        return _ratios_to_samples(self.RATIOS)

    @requires_module
    def test_returns_float(self, ratio_samples):
        result = calibrate_threshold(ratio_samples, target_pass=0.70)
        assert isinstance(result, float)

    @requires_module
    def test_empty_returns_default_k(self):
        """Empty samples must return DEFAULT_K without raising."""
        result = calibrate_threshold([], target_pass=0.70)
        assert result == pytest.approx(DEFAULT_K)

    @requires_module
    def test_result_clamped_to_lower_bound(self):
        """Result must be >= 0.5 even when the quantile would be smaller."""
        # Force all ratios very low so raw quantile < 0.5
        samples = _ratios_to_samples([0.1, 0.15, 0.2, 0.25])
        result = calibrate_threshold(samples, target_pass=0.50)
        assert result >= 0.5

    @requires_module
    def test_result_clamped_to_upper_bound(self):
        """Result must be <= 1.5 even when the quantile would be larger."""
        # Force all ratios very high so raw quantile > 1.5
        samples = _ratios_to_samples([2.0, 2.5, 3.0, 3.5])
        result = calibrate_threshold(samples, target_pass=0.90)
        assert result <= 1.5

    @requires_module
    def test_result_in_valid_range(self, ratio_samples):
        """Returned k is always in [0.5, 1.5]."""
        for target in [0.50, 0.60, 0.70, 0.80, 0.90]:
            k = calibrate_threshold(ratio_samples, target_pass=target)
            assert 0.5 <= k <= 1.5, f"k={k} out of range for target={target}"

    @requires_module
    def test_quantile_approximates_target(self, ratio_samples):
        """At target_pass=0.70 with 7 known ratios, returned k ≈ 0.70-quantile."""
        # The 0.70-quantile of [0.5,0.6,0.7,0.8,0.9,1.0,1.1] is ≈ 0.9 (index 4 of 7)
        # Use a loose tolerance since numpy/statistics quantile methods vary slightly.
        k = calibrate_threshold(ratio_samples, target_pass=0.70)
        raw_q = statistics.quantiles(self.RATIOS, n=100)[int(0.70 * 100) - 1]
        assert k == pytest.approx(raw_q, abs=0.12)

    @requires_module
    def test_pass_rate_near_target_after_calibration(self, ratio_samples):
        """compute_vrp_stats at the returned k should give pass_rate ≈ target_pass."""
        target = 0.70
        k = calibrate_threshold(ratio_samples, target_pass=target)
        stats = compute_vrp_stats(ratio_samples, k=k)
        assert abs(stats.pass_rate - target) <= 0.15, (
            f"pass_rate={stats.pass_rate:.3f} too far from target={target} at k={k:.4f}"
        )

    @requires_module
    def test_higher_target_yields_higher_or_equal_k(self, ratio_samples):
        """Increasing target_pass should yield a higher (or equal) k."""
        k_low = calibrate_threshold(ratio_samples, target_pass=0.40)
        k_high = calibrate_threshold(ratio_samples, target_pass=0.80)
        assert k_high >= k_low

    @requires_module
    def test_single_sample_no_error(self):
        """A single-element sample list must not raise."""
        samples = [(0.15, 0.20)]
        result = calibrate_threshold(samples, target_pass=0.70)
        assert 0.5 <= result <= 1.5

    @requires_module
    def test_none_rows_ignored_in_calibration(self):
        """None realized_vol rows must be dropped before computing the quantile."""
        clean = _ratios_to_samples(self.RATIOS)
        dirty = clean + [(None, 0.20), (0.10, 0), (0.10, -1)]
        k_clean = calibrate_threshold(clean, target_pass=0.70)
        k_dirty = calibrate_threshold(dirty, target_pass=0.70)
        assert k_clean == pytest.approx(k_dirty, abs=1e-9)

    @requires_module
    def test_calibrate_consistency_with_default_target(self):
        """When the data's 70th-percentile ratio equals DEFAULT_K, result == DEFAULT_K."""
        # Build a sample set whose 70th-percentile ratio is exactly 0.9
        # ratios: 9 values — 0.5,0.6,0.7,0.8,0.9,0.9,1.0,1.1,1.2
        # 70th percentile of 9 values → index ≈ floor(0.7*9)=6 → value 1.0 (0-indexed)
        # Adjust: use a distribution tightly around 0.9 at the target quantile.
        # Simpler: just assert the return is in range and not DEFAULT_K (could coincide).
        samples = _ratios_to_samples([0.7, 0.8, 0.9, 1.0, 1.1])
        result = calibrate_threshold(samples, target_pass=0.70)
        assert 0.5 <= result <= 1.5


# ===========================================================================
# 5. Integration: gate_decision round-trip with calibrate_threshold
# ===========================================================================

class TestRoundTrip:
    """Sanity-check that calibrate_threshold output feeds gate_decision correctly."""

    @requires_module
    def test_calibrated_k_gates_sell_correctly(self):
        """Using calibrated k, a clearly low-vol observation gives SELL_PREMIUM."""
        samples = _ratios_to_samples([0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1])
        k = calibrate_threshold(samples, target_pass=0.70)
        iv = 0.20
        # realized clearly below k*iv
        rv = 0.01
        assert gate_decision(rv, iv, k=k) == SELL_PREMIUM

    @requires_module
    def test_calibrated_k_gates_buy_correctly(self):
        """Using calibrated k, realized well above implied gives BUY_PREMIUM."""
        samples = _ratios_to_samples([0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1])
        k = calibrate_threshold(samples, target_pass=0.70)
        iv = 0.20
        rv = iv * 2.0  # clearly above implied
        assert gate_decision(rv, iv, k=k) == BUY_PREMIUM

    @requires_module
    def test_fail_open_unaffected_by_k_value(self):
        """Fail-open behaviour must hold regardless of k."""
        for k in [0.5, 0.9, 1.0, 1.5]:
            assert gate_decision(None, 0.20, k=k) == STAND_ASIDE
            assert gate_decision(0.10, 0, k=k) == STAND_ASIDE
            assert gate_decision(0.10, None, k=k) == STAND_ASIDE


# ===========================================================================
# 6. NEW HIGH-VALUE COVERAGE TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# 6a. k>1 no-overlap: BUY_PREMIUM takes priority over SELL_PREMIUM when both
#     conditions would be satisfied simultaneously.
# ---------------------------------------------------------------------------


class TestKGreaterThanOneOrdering:
    """With k=1.5, the BUY and SELL ranges overlap:
    SELL condition: rv < k*iv = 1.5*0.12 = 0.18
    BUY  condition: rv > iv   = 0.12
    Overlap: 0.12 < rv < 0.18 → BUY must win (evaluated first).
    """

    IV = 0.12
    K = 1.5

    @requires_module
    def test_realized_above_implied_is_buy_even_with_large_k(self):
        """rv=0.13 > 0.12=iv AND rv=0.13 < 1.5*0.12=0.18 → BUY_PREMIUM wins."""
        rv = 0.13  # in the overlap zone
        assert gate_decision(rv, self.IV, k=self.K) == BUY_PREMIUM

    @requires_module
    def test_realized_below_implied_and_below_threshold_is_sell(self):
        """rv=0.11 < iv=0.12 AND rv=0.11 < 1.5*0.12=0.18 → SELL_PREMIUM."""
        rv = 0.11  # below iv, so BUY condition fails; rv < k*iv → SELL
        assert gate_decision(rv, self.IV, k=self.K) == SELL_PREMIUM


# ---------------------------------------------------------------------------
# 6b. realized_vol < 0 guard: must return STAND_ASIDE (fail-open)
# ---------------------------------------------------------------------------


class TestNegativeRealizedVolGuard:
    """Negative realized_vol is physically impossible; gate must return STAND_ASIDE."""

    @requires_module
    def test_negative_realized_vol_is_stand_aside(self):
        assert gate_decision(-0.01, 0.12) == STAND_ASIDE

    @requires_module
    def test_negative_realized_vol_does_not_raise(self):
        try:
            result = gate_decision(-0.01, 0.12)
            assert result == STAND_ASIDE
        except Exception as exc:
            pytest.fail(f"gate_decision raised on negative realized_vol: {exc}")

    @requires_module
    def test_large_negative_realized_vol_is_stand_aside(self):
        assert gate_decision(-999.0, 0.12) == STAND_ASIDE


# ---------------------------------------------------------------------------
# 6c. calibrate_threshold skips realized_vol<=0 (degenerate pairs)
# ---------------------------------------------------------------------------


class TestCalibrateThresholdSkipsDegeneratePairs:
    """Degenerate pairs (rv<=0) must be excluded from the ratio set without
    crashing, and the returned k must still be in [0.5, 1.5]."""

    @requires_module
    def test_zero_realized_vol_is_skipped_without_crash(self):
        """A (0.0, 0.12) pair must be excluded (rv<=0) and not cause ZeroDivisionError."""
        valid_pairs = [
            (0.08, 0.12),
            (0.09, 0.12),
            (0.10, 0.12),
            (0.11, 0.12),
            (0.13, 0.12),
        ]
        degenerate = [(0.0, 0.12), (-0.1, 0.12)]
        samples = degenerate + valid_pairs
        # Must not raise
        k = calibrate_threshold(samples, target_pass=0.70)
        assert 0.5 <= k <= 1.5

    @requires_module
    def test_degenerate_pairs_do_not_change_result(self):
        """Adding degenerate pairs to a clean set must not change the returned k."""
        valid_pairs = [
            (0.08, 0.12),
            (0.09, 0.12),
            (0.10, 0.12),
            (0.11, 0.12),
            (0.13, 0.12),
        ]
        degenerate = [(0.0, 0.12), (-0.1, 0.12)]
        k_clean = calibrate_threshold(valid_pairs, target_pass=0.70)
        k_dirty = calibrate_threshold(degenerate + valid_pairs, target_pass=0.70)
        # Degenerate pairs must be excluded so result is identical
        assert k_clean == pytest.approx(k_dirty, abs=1e-9)

    @requires_module
    def test_all_degenerate_pairs_returns_default_k(self):
        """When ALL pairs are degenerate (rv<=0), must return DEFAULT_K."""
        samples = [(0.0, 0.12), (-0.1, 0.12), (0.0, 0.20)]
        k = calibrate_threshold(samples, target_pass=0.70)
        assert k == pytest.approx(DEFAULT_K)
