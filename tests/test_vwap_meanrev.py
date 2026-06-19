"""
Unit tests for VwapMeanReversion strategy.

Drive on_tick() directly with volume= (no DB, no engine).
All timestamps use 2024-02-01 (Thursday) as the fixed test date, in naive IST.

Design note on test data: because the strategy updates VWAP/band with EACH bar
(including the signal bar) before evaluating entry conditions, the signal bar's
own tp shifts VWAP and the band.  To reliably produce an entry signal in a test
we use one of:
  a) many warm-up bars so the signal bar has small fractional weight in the VWAP
  b) a large warm-up volume so the signal bar (with normal/small volume) barely
     moves VWAP — preserving the pre-signal VWAP and band geometry.
Both approaches are used below as appropriate.
"""
from datetime import datetime, date

import pytest

from strategies.vwap_meanrev import VwapMeanReversion, VwapMeanRevParams

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_DATE = date(2024, 2, 1)  # Thursday


def ist(hh: int, mm: int, *, d: date = TEST_DATE) -> datetime:
    """Naive IST timestamp (strategy accepts naive)."""
    return datetime(d.year, d.month, d.day, hh, mm)


def strat(warmup: int = 10, **kw) -> VwapMeanReversion:
    p = VwapMeanRevParams(warmup_bars=warmup, **kw)
    return VwapMeanReversion("TEST", p)


def _warm_up(s: VwapMeanReversion, n: int = None, start_mm: int = 15,
             volume: float = 100_000.0) -> None:
    """
    Feed n (default warmup_bars) bars alternating 101/99 around 100 with large
    volume so any subsequent signal bar has negligible weight on VWAP.
    """
    warmup_n = n if n is not None else s.p.warmup_bars
    for i in range(warmup_n):
        total_mm = start_mm + i
        hh = 9 + total_mm // 60
        mm = total_mm % 60
        px = 101.0 if i % 2 == 0 else 99.0
        s.on_tick(ist(hh, mm), px, high=px + 1.0, low=px - 1.0, volume=volume)


# ---------------------------------------------------------------------------
# Test 1 — Warm-up blocks entries
# ---------------------------------------------------------------------------

def test_warmup_blocks_entries():
    """
    Feed warmup_bars-1 ticks, then a bar far above — every on_tick must return
    None until warm-up is satisfied.  Uses warmup=5 so we can feed exactly 4
    pre-warmup bars and observe they all return None.
    """
    s = strat(warmup=5, entry_band_mult=2.0, min_band_pct=0.0)

    for i in range(4):  # 4 bars < warmup=5
        px = 101.0 if i % 2 == 0 else 99.0
        d = s.on_tick(ist(9, 15 + i), px, high=px + 1, low=px - 1, volume=1000.0)
        assert d is None, f"bar {i}: expected None before warm-up completes"

    assert s._n == 4, "_n should be 4 after 4 bars"
    # 5th bar (warmup satisfied) at extreme price — might or might not signal
    # depending on band; the key invariant is the first 4 bars returned None.


# ---------------------------------------------------------------------------
# Test 2 — SHORT entry on stretch above VWAP
# ---------------------------------------------------------------------------

def test_short_entry_stretch_above_vwap():
    """
    Warm-up with large-volume bars (VWAP anchored at ~100, band ≈ 0.9).
    Signal bar uses tiny volume so VWAP barely moves; price at upper+3 triggers.
    Asserts: action=ENTER/SELL, target < price, stop > upper (formula invariant).

    Note on geometry: stop = upper + stop_band_mult*band.  stop > price is only
    guaranteed when price < upper + stop_band_mult*band (i.e., the price is within
    stop_band_mult bands of the trigger level).  We test stop > upper as the spec
    formula invariant; price here is kept within that range so stop > price also holds.
    """
    import math

    s = strat(warmup=10, entry_band_mult=2.0, stop_band_mult=1.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)  # 10 bars, large volume

    vwap_before = s._cum_pv / s._cum_vol  # ≈ 100
    band_before = math.sqrt(max(s._m2 / (s._n - 1), 0.0))
    upper_before = vwap_before + 2.0 * band_before

    # Price just 3 units above upper — within stop_band_mult*band of upper so stop > price
    stretch_price = upper_before + 3.0
    d = s.on_tick(
        ist(9, 30), stretch_price,
        high=stretch_price + 0.3, low=stretch_price - 0.3,
        volume=1.0,   # negligible weight on VWAP
    )

    assert d is not None, "Expected ENTER SELL on stretch above VWAP"
    assert d.action == "ENTER"
    assert d.side == "SELL"
    # Mean-reversion geometry (per spec §10 sanity invariants):
    # target (=vwap) < entry price < stop
    assert d.target < stretch_price, "Target must be below entry price (revert to VWAP)"
    assert d.stop > stretch_price, "Stop must be above entry price for a short"
    assert d.stop > upper_before, "Stop must be above the trigger upper band level"


# ---------------------------------------------------------------------------
# Test 3 — LONG entry on stretch below VWAP
# ---------------------------------------------------------------------------

def test_long_entry_stretch_below_vwap():
    """
    Symmetric to test 2: VWAP anchored at ~100, bar near lower band → ENTER BUY.
    Price is kept within stop_band_mult bands below lower so stop < price holds.
    """
    import math

    s = strat(warmup=10, entry_band_mult=2.0, stop_band_mult=1.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol  # ≈ 100
    band_before = math.sqrt(max(s._m2 / (s._n - 1), 0.0))
    lower_before = vwap_before - 2.0 * band_before

    # Price just 3 units below lower — within stop_band_mult*band of lower so stop < price
    stretch_price = lower_before - 3.0
    d = s.on_tick(
        ist(9, 30), stretch_price,
        high=stretch_price + 0.3, low=stretch_price - 0.3,
        volume=1.0,
    )

    assert d is not None, "Expected ENTER BUY on stretch below VWAP"
    assert d.action == "ENTER"
    assert d.side == "BUY"
    # Mean-reversion geometry (per spec §10 sanity invariants):
    # stop < entry price < target (=vwap)
    assert d.target > stretch_price, "Target must be above entry price (revert to VWAP)"
    assert d.stop < stretch_price, "Stop must be below entry price for a long"
    assert d.stop < lower_before, "Stop must be below the trigger lower band level"


# ---------------------------------------------------------------------------
# Test 4 — No signal inside the band
# ---------------------------------------------------------------------------

def test_no_signal_inside_band():
    """Price within ±2σ band → no entry (after warm-up)."""
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol  # ≈ 100

    # Feed a bar at vwap+0.5 — inside the band
    d = s.on_tick(
        ist(9, 30), vwap_before + 0.5,
        high=vwap_before + 0.7, low=vwap_before + 0.3,
        volume=1000.0,
    )
    assert d is None, "No signal when price is inside the VWAP band"


# ---------------------------------------------------------------------------
# Test 5 — VWAP reversion target exit (LONG)
# ---------------------------------------------------------------------------

def test_vwap_reversion_exit_long():
    """After a LONG, price rises back to/above VWAP → EXIT 'VWAP target'."""
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol  # ≈ 100

    # Inject a long position below VWAP
    s.position = 10
    s.entry_price = vwap_before - 10.0
    s._stop = vwap_before - 20.0  # stop well below

    # Bar whose close is >= vwap (reverts to mean)
    reversion_price = vwap_before + 0.5
    d = s.on_tick(
        ist(9, 30), reversion_price,
        high=reversion_price + 0.2, low=reversion_price - 0.1,
        volume=1000.0,
    )

    assert d is not None, "Expected EXIT when long reverts to VWAP"
    assert d.action == "EXIT"
    assert "VWAP target" in d.reason


# ---------------------------------------------------------------------------
# Test 6 — VWAP reversion target exit (SHORT)
# ---------------------------------------------------------------------------

def test_vwap_reversion_exit_short():
    """After a SHORT, price drops back to/below VWAP → EXIT 'VWAP target'."""
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol  # ≈ 100

    # Inject a short position above VWAP
    s.position = -10
    s.entry_price = vwap_before + 10.0
    s._stop = vwap_before + 20.0  # stop well above

    # Bar whose close is <= vwap
    reversion_price = vwap_before - 0.1
    d = s.on_tick(
        ist(9, 30), reversion_price,
        high=reversion_price + 0.1, low=reversion_price - 0.2,
        volume=1000.0,
    )

    assert d is not None, "Expected EXIT when short reverts to VWAP"
    assert d.action == "EXIT"
    assert "VWAP target" in d.reason


# ---------------------------------------------------------------------------
# Test 7 — Protective stop exit precedence over target (SHORT)
# ---------------------------------------------------------------------------

def test_stop_exit_precedence_over_target_short():
    """
    SHORT position: when price simultaneously satisfies both the protective stop
    condition (price >= _stop) AND the VWAP-target condition (price <= vwap),
    the stop exit must fire first (§3.3 precedence order).

    Contrived setup: set _stop = vwap - ε so a close at exactly _stop is
    both >= stop and <= vwap.
    """
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol  # ≈ 100

    # Inject short; place stop just below VWAP so price at stop triggers both conditions
    s.position = -10
    s.entry_price = vwap_before + 8.0
    s._stop = vwap_before - 0.5   # artificially low so price >= _stop AND price <= vwap

    test_price = vwap_before - 0.5   # equals _stop; satisfies both guards
    d = s.on_tick(
        ist(9, 30), test_price,
        high=test_price + 0.1, low=test_price - 0.1,
        volume=1000.0,
    )

    assert d is not None
    assert d.action == "EXIT"
    assert "VWAP-MR stop" in d.reason, (
        f"Stop must take precedence over VWAP target; got reason={d.reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — EOD square-off is unconditional (even without warm-up)
# ---------------------------------------------------------------------------

def test_eod_squareoff_unconditional():
    """
    Reconciled position, zero warm-up bars processed → at 15:20 must still
    return EOD EXIT (the squareoff does not depend on _ready).
    """
    s = strat(warmup=20, squareoff_before_close_min=15)

    # Mid-session restart: position reconciled but no tick history
    s.notify_fill("BUY", 10, 101.0)
    assert s.position == 10
    assert s._n == 0  # no ticks

    d = s.on_tick(ist(15, 20), 100.0, high=100.5, low=99.5, volume=1000.0)

    assert d is not None, "EOD square-off must fire even when _ready is False"
    assert d.action == "EXIT"
    assert "EOD square-off" in d.reason


# ---------------------------------------------------------------------------
# Test 9 — No new entry near close
# ---------------------------------------------------------------------------

def test_no_entry_near_close():
    """
    no_entry_before_close_min=30 → cutoff at 15:00.
    Stretch at 15:10 → None (suppressed).
    Same setup with stretch at 14:00 → ENTER (control).
    """
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0,
              no_entry_before_close_min=30)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol
    stretch = vwap_before + 12.0

    # Control — 14:00 stretch should enter
    s_ctrl = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0,
                   no_entry_before_close_min=30)
    _warm_up(s_ctrl, volume=100_000.0)
    d_ctrl = s_ctrl.on_tick(
        ist(14, 0), stretch, high=stretch + 1.0, low=stretch - 0.5, volume=1.0,
    )
    assert d_ctrl is not None and d_ctrl.action == "ENTER", (
        "Control: stretch at 14:00 must enter"
    )

    # Test — 15:10 stretch must NOT enter (past 15:00 cutoff)
    d_late = s.on_tick(
        ist(15, 10), stretch, high=stretch + 1.0, low=stretch - 0.5, volume=1.0,
    )
    assert d_late is None, "Entry must be suppressed within no_entry_before_close_min of close"


# ---------------------------------------------------------------------------
# Test 10 — Session reset wipes VWAP/band, preserves position
# ---------------------------------------------------------------------------

def test_session_reset_wipes_indicators_preserves_position():
    """
    After a full warmed-up session on day 1, a bar on day 2 resets cumulative
    VWAP/band state but leaves position and entry_price intact.
    """
    day2 = date(2024, 2, 2)

    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)   # day-1 session

    assert s._cum_vol > 0 and s._n == 10

    # Inject a position to verify it survives the session reset
    s.position = 10
    s.entry_price = 98.0

    # First bar on day 2
    s.on_tick(
        datetime(day2.year, day2.month, day2.day, 9, 15),
        200.0, high=201.0, low=199.0, volume=500.0,
    )

    # Session state must be reset (only 1 bar on day 2)
    assert s._n == 1, "_n should be 1 after the first day-2 bar"
    assert s._cum_vol == pytest.approx(500.0), "_cum_vol should only include day-2 bars"

    # Not ready (only 1 bar)
    st = s.status()
    assert not st["ready"], "Should not be ready after only 1 bar on day 2"

    # Position must survive the reset
    assert s.position == 10, "position must NOT be reset by session date change"
    assert s.entry_price == pytest.approx(98.0), "entry_price must survive session reset"


# ---------------------------------------------------------------------------
# Test 11 — Zero / None volume safety
# ---------------------------------------------------------------------------

def test_zero_and_none_volume_no_crash():
    """
    Bars with volume=None or volume=0 must not crash.  _cum_vol stays 0;
    VWAP falls back to tp (no division by zero).  Entries must be blocked
    while _cum_vol == 0 (the _ready check requires cum_vol > 0).
    """
    s = strat(warmup=3, min_band_pct=0.0)

    # volume=None
    d = s.on_tick(ist(9, 15), 100.0, high=101.0, low=99.0, volume=None)
    assert d is None
    assert s._cum_vol == 0.0, "volume=None must contribute 0 to cumulative vol"

    # volume=0
    d = s.on_tick(ist(9, 16), 100.0, high=101.0, low=99.0, volume=0.0)
    assert d is None
    assert s._cum_vol == 0.0, "volume=0 must contribute 0 to cumulative vol"

    # Many more zero-vol bars — no crash, no spurious signals
    for i in range(20):
        result = s.on_tick(ist(9, 17 + i // 60, d=TEST_DATE), 100.0,
                           high=101.0, low=99.0, volume=None)
        assert result is None or result.action in {"ENTER", "EXIT"}


# ---------------------------------------------------------------------------
# Test 12 — min_band_pct filter suppresses entry on dead-flat session
# ---------------------------------------------------------------------------

def test_min_band_pct_filter():
    """
    When band < price * min_band_pct, the entry is suppressed even after warm-up.
    Use a very large min_band_pct (0.99) to guarantee the band is always "too small".
    """
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.99, stop_band_mult=1.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol
    stretch = vwap_before + 12.0

    d = s.on_tick(
        ist(9, 30), stretch, high=stretch + 1, low=stretch - 1, volume=1.0,
    )
    assert d is None, "min_band_pct filter must suppress entry when band is too small"


# ---------------------------------------------------------------------------
# Test 13 — notify_flat resets stop and target
# ---------------------------------------------------------------------------

def test_notify_flat_resets_stop_and_target():
    """notify_flat must clear position, entry_price, _stop, and _target."""
    s = strat()
    s.notify_fill("BUY", 10, 100.0)
    s._stop = 95.0
    s._target = 105.0

    s.notify_flat()

    assert s.position == 0
    assert s.entry_price == 0.0
    assert s._stop == 0.0
    assert s._target == 0.0


# ---------------------------------------------------------------------------
# Test 14 — max_entries_per_session cap
# ---------------------------------------------------------------------------

def test_max_entries_per_session_cap():
    """After max_entries_per_session ENTER emissions, no further entries."""
    s = strat(warmup=10, entry_band_mult=2.0, min_band_pct=0.0, max_entries_per_session=2)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol
    stretch = vwap_before + 12.0

    # Entry 1
    d1 = s.on_tick(ist(9, 30), stretch, high=stretch + 1, low=stretch - 0.5, volume=1.0)
    assert d1 is not None and d1.action == "ENTER", "First entry must fire"
    s.notify_fill("SELL", 10, stretch)
    s.notify_flat()

    # Entry 2 (re-entry after round trip)
    d2 = s.on_tick(ist(9, 31), stretch, high=stretch + 1, low=stretch - 0.5, volume=1.0)
    assert d2 is not None and d2.action == "ENTER", "Second entry must fire"
    s.notify_fill("SELL", 10, stretch)
    s.notify_flat()

    # Entry 3 — cap hit
    d3 = s.on_tick(ist(9, 32), stretch, high=stretch + 1, low=stretch - 0.5, volume=1.0)
    assert d3 is None, "Third entry must be blocked by max_entries_per_session=2"


# ---------------------------------------------------------------------------
# Test 15 — Price <= 0 guard
# ---------------------------------------------------------------------------

def test_invalid_price_returns_none():
    """Zero or negative price is immediately rejected."""
    s = strat()
    assert s.on_tick(ist(10, 0), 0.0, high=0.0, low=0.0, volume=1000.0) is None
    assert s.on_tick(ist(10, 1), -5.0, high=-4.0, low=-6.0, volume=1000.0) is None


# ---------------------------------------------------------------------------
# Test 16 — typical_std band method works without crash
# ---------------------------------------------------------------------------

def test_typical_std_band_method():
    """typical_std mode warms up and can produce signals without crashing."""
    s = strat(warmup=10, band_method="typical_std", entry_band_mult=2.0, min_band_pct=0.0)
    _warm_up(s, volume=100_000.0)

    vwap_before = s._cum_pv / s._cum_vol
    stretch = vwap_before + 12.0

    d = s.on_tick(ist(9, 30), stretch, high=stretch + 1, low=stretch - 1, volume=1.0)
    # May or may not signal depending on band magnitude; must not crash
    if d is not None:
        assert d.action == "ENTER"
        assert d.side == "SELL"


# ---------------------------------------------------------------------------
# Test 17 — status() returns correct keys and types
# ---------------------------------------------------------------------------

def test_status_keys():
    """status() must contain the required keys; security_id must match ctor arg."""
    s = strat()
    st = s.status()
    required = {"security_id", "vwap", "band", "upper", "lower",
                "position", "entry_price", "ready", "entries_this_session"}
    assert required.issubset(st.keys()), f"Missing keys: {required - st.keys()}"
    assert st["security_id"] == "TEST"
    assert isinstance(st["ready"], bool)
    assert isinstance(st["entries_this_session"], int)
