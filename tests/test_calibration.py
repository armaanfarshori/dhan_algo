"""Calibration math — hit logic, threshold sweep, recommendation, gate value."""
from ml.calibration import (bucketize, directional_hit, gate_value_summary,
                            recommend_threshold, threshold_sweep)


def test_directional_hit():
    assert directional_hit("BUY", 0.01) is True
    assert directional_hit("BUY", -0.01) is False
    assert directional_hit("SELL", -0.01) is True
    assert directional_hit("SELL", 0.01) is False
    assert directional_hit("HOLD", 0.01) is None


def rows(spec):
    """spec: list of (confidence, hit)."""
    return [{"confidence": c, "hit": h} for c, h in spec]


def test_threshold_sweep_monotone_n():
    rs = rows([(0.2, True), (0.5, False), (0.8, True)])
    sweep = threshold_sweep(rs, thresholds=[0.0, 0.4, 0.7])
    assert [s["n"] for s in sweep] == [3, 2, 1]
    assert sweep[2]["accuracy"] == 1.0


def test_recommend_threshold_picks_lowest_qualifying():
    # 40 rows at conf 0.3 with 60% acc; 40 at 0.6 with 70% acc
    rs = rows([(0.3, i < 24) for i in range(40)] + [(0.6, i < 28) for i in range(40)])
    sweep = threshold_sweep(rs, thresholds=[0.0, 0.3, 0.6])
    rec = recommend_threshold(sweep, min_n=30, min_acc=0.55)
    assert rec == 0.0      # whole population already ≥55% with n≥30


def test_recommend_threshold_none_when_thin():
    rs = rows([(0.9, True)] * 10)   # perfect but only 10 samples
    rec = recommend_threshold(threshold_sweep(rs), min_n=30)
    assert rec is None


def test_recommend_threshold_none_when_inaccurate():
    rs = rows([(0.5, i % 2 == 0) for i in range(100)])   # 50% = coin flip
    assert recommend_threshold(threshold_sweep(rs)) is None


def gate_rows(allow_rets, block_rets):
    out = []
    for r in allow_rets:
        out.append({"verdict": "ALLOW", "requested_direction": "BUY", "realized_return": r})
    for r in block_rets:
        out.append({"verdict": "BLOCK", "requested_direction": "BUY", "realized_return": r})
    return out


def test_gate_value_allow_better():
    g = gate_value_summary(gate_rows([0.01] * 40, [-0.01] * 40))
    assert g["allow"]["hit_rate"] == 1.0
    assert g["block"]["hit_rate"] == 0.0
    assert g["gate_adds_value"] is True


def test_gate_value_insufficient_n():
    g = gate_value_summary(gate_rows([0.01] * 5, [-0.01] * 5))
    assert g["gate_adds_value"] is None     # <30 per group → no verdict


def test_gate_value_short_direction_sign():
    # SELL breakout: a NEGATIVE raw return is a win for the breakout
    rws = [{"verdict": "ALLOW", "requested_direction": "SELL", "realized_return": -0.02}]
    g = gate_value_summary(rws)
    assert g["allow"]["hit_rate"] == 1.0
    assert g["allow"]["avg_return_bps"] == 200.0


def test_bucketize():
    rs = rows([(0.05, True), (0.08, False), (0.95, True)])
    b = bucketize(rs, width=0.1)
    assert b[0]["bucket"].startswith("0.00") and b[0]["n"] == 2
    assert b[-1]["n"] == 1 and b[-1]["accuracy"] == 1.0
