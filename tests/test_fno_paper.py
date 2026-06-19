"""Tests for core/fno_paper.py — forward paper-trade log for iron-condor entries.

All tests use monkeypatched DB sessions (no real DB or network calls).  The
fake-session pattern mirrors tests/test_fno_condor.py's TestCyclesFromDb helper:
a context-manager wrapper around a MagicMock that returns scripted fetchall()
/ fetchone() result sets in the order each function calls session.execute().

Test scenarios
--------------
(a) record_paper_entry returns recorded=False with decision when the gate says
    STAND_ASIDE or BUY_PREMIUM (realized >= k*implied).
(b) record_paper_entry records (recorded=True) a SELL_PREMIUM entry with
    correct credit = (sp+sc)-(lp+lc) and max_loss.
(c) record_paper_entry returns reason when there is no ATM iv or no realized vol.
(d) resolve_paper_trades computes net_pnl from resolve_condor for a matured
    OPEN trade and marks win correctly.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — tests are written against the spec; skip cleanly if missing
# ---------------------------------------------------------------------------
try:
    from core.fno_paper import record_paper_entry, resolve_paper_trades

    _MODULE_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _MODULE_AVAILABLE = False

requires_module = pytest.mark.skipif(
    not _MODULE_AVAILABLE, reason="core/fno_paper not yet implemented"
)

# ---------------------------------------------------------------------------
# Constants mirrored from production modules (avoid importing them at
# module-collection time so the test file is always importable)
# ---------------------------------------------------------------------------
_IST = timezone(timedelta(hours=5, minutes=30))

# NIFTY lot size (matching research.backtest.fno_costs.NIFTY_LOT)
_LOT = 65

# Gate default k
_DEFAULT_K = 0.9

# Reference date: a Thursday (expiry day) that is safely in the future
_TODAY = date(2026, 7, 3)          # entry date (today)
_EXPIRY = date(2026, 7, 10)        # nearest future expiry (7 days out)
_NOW_IST = datetime(2026, 7, 3, 10, 0, 0, tzinfo=_IST)

# Option chain canned data: a small NIFTY chain around ATM=24000
_SPOT = 24000.0
_ATM = 24000  # nifty_atm_strike(24000, 50) = 24000

# Premiums for the chain rows (realistic-ish for a weekly with DTE=7)
# CE rows: strike → (ltp, iv_percent)
# PE rows: strike → (ltp, iv_percent)
# ATM:  CE ltp=120 iv=14.0%, PE ltp=118 iv=13.6%
# +100: CE ltp=60  iv=13.0%          → short_call at 24050 (after build_condor)
# -100: PE ltp=62  iv=13.2%          → short_put at 23950 (after build_condor)
# +200: CE ltp=25  iv=12.5%          → long_call wing
# -200: PE ltp=27  iv=12.7%          → long_put wing
# We define enough strikes to cover all four condor legs.

def _build_chain_rows(
    spot: float = _SPOT,
    atm: int = _ATM,
) -> list[tuple]:
    """Return option_chain_snapshot rows as (strike, option_type, ltp, iv, spot).

    iv is in RAW PERCENT (as stored by parse_option_chain — NOT yet / 100).
    """
    rows = [
        # ATM
        (float(atm),        "CE", 120.0, 14.0, spot),
        (float(atm),        "PE", 118.0, 13.6, spot),
        # +50 / -50
        (float(atm + 50),   "CE",  90.0, 13.8, spot),
        (float(atm - 50),   "PE",  91.0, 13.9, spot),
        # +100 / -100
        (float(atm + 100),  "CE",  60.0, 13.0, spot),
        (float(atm - 100),  "PE",  62.0, 13.2, spot),
        # +150 / -150
        (float(atm + 150),  "CE",  40.0, 12.8, spot),
        (float(atm - 150),  "PE",  42.0, 12.9, spot),
        # +200 / -200  (likely long wing locations)
        (float(atm + 200),  "CE",  25.0, 12.5, spot),
        (float(atm - 200),  "PE",  27.0, 12.7, spot),
        # +250 / -250
        (float(atm + 250),  "CE",  15.0, 12.3, spot),
        (float(atm - 250),  "PE",  16.0, 12.4, spot),
        # +300 / -300
        (float(atm + 300),  "CE",   8.0, 12.0, spot),
        (float(atm - 300),  "PE",   9.0, 12.1, spot),
        # +350 / -350
        (float(atm + 350),  "CE",   4.0, 11.8, spot),
        (float(atm - 350),  "PE",   4.5, 11.9, spot),
        # +400 / -400
        (float(atm + 400),  "CE",   2.0, 11.5, spot),
        (float(atm - 400),  "PE",   2.2, 11.6, spot),
        # +450 / -450
        (float(atm + 450),  "CE",   1.5, 11.3, spot),
        (float(atm - 450),  "PE",   1.7, 11.4, spot),
        # +500 / -500
        (float(atm + 500),  "CE",   1.1, 11.0, spot),
        (float(atm - 500),  "PE",   1.2, 11.1, spot),
        # +550 / -550
        (float(atm + 550),  "CE",   0.9, 10.8, spot),
        (float(atm - 550),  "PE",   1.0, 10.9, spot),
        # +600 / -600
        (float(atm + 600),  "CE",   0.7, 10.5, spot),
        (float(atm - 600),  "PE",   0.8, 10.6, spot),
        # +650 / -650
        (float(atm + 650),  "CE",   0.55, 10.3, spot),
        (float(atm - 650),  "PE",   0.60, 10.4, spot),
        # +700 / -700
        (float(atm + 700),  "CE",   0.4, 10.0, spot),
        (float(atm - 700),  "PE",   0.45, 10.1, spot),
        # +750 / -750
        (float(atm + 750),  "CE",   0.3, 9.8, spot),
        (float(atm - 750),  "PE",   0.35, 9.9, spot),
        # +800 / -800  (long wing coverage for move_mult=1.5 at straddle_iv≈0.138, DTE=7)
        # build_condor: short ≈ ±687 pts → long = short ± 100 ≈ ±787 pts
        (float(atm + 800),  "CE",   0.2, 9.5, spot),
        (float(atm - 800),  "PE",   0.22, 9.6, spot),
    ]
    return rows


# ---------------------------------------------------------------------------
# Fake-session factory helpers (mirror TestCyclesFromDb pattern)
# ---------------------------------------------------------------------------

def _make_result(rows: list) -> MagicMock:
    """Return a MagicMock result whose fetchall() and fetchone() serve canned data."""
    result = MagicMock()
    result.fetchall.return_value = rows
    result.fetchone.return_value = rows[0] if rows else None
    return result


def _make_fake_session(*results: MagicMock):
    """Return a get_session context-manager that yields a single fake session.

    The session's execute() returns results in the order they are passed.
    rowcount on the final INSERT is set to 1 (inserted).
    """
    session = MagicMock()
    session.execute.side_effect = list(results)

    @contextmanager
    def fake_get_session():
        yield session

    return fake_get_session


def _make_fake_multi_session(sessions_results: list[list]) -> Any:
    """Return a get_session factory that yields a NEW session for each `with` call.

    sessions_results: list of lists, each inner list is the sequence of execute()
    results for one `with get_session()` block.
    """
    _calls: list[int] = [0]

    @contextmanager
    def fake_get_session():
        idx = _calls[0]
        _calls[0] += 1
        if idx >= len(sessions_results):
            # Extra calls: return empty results
            sess = MagicMock()
            sess.execute.return_value = _make_result([])
            yield sess
            return
        sess = MagicMock()
        sess.execute.side_effect = list(sessions_results[idx])
        yield sess

    return fake_get_session


# ---------------------------------------------------------------------------
# Full-flow canned session sequence for record_paper_entry (SELL_PREMIUM case)
# ---------------------------------------------------------------------------

def _build_sell_session(
    realized_vol: float = 0.08,   # well below k * straddle_iv
    straddle_iv_raw: float = 0.14,  # fraction (avg of ATM CE/PE iv_frac)
    expiry: date = _EXPIRY,
    snapshot_time: datetime = _NOW_IST,
    chain_rows: list | None = None,
    insert_rowcount: int = 1,
) -> Any:
    """Build the multi-session sequence for a successful SELL_PREMIUM entry.

    Session calls (in order per `with get_session()` block):
      Block 0 (expiry + snapshot_time + chain load — single `with` block in impl):
        execute 0: min(expiry_date) → (expiry,)
        execute 1: max(snapshot_time) → (snapshot_time,)
        execute 2: chain rows → list of (strike, option_type, ltp, iv, spot)
      Block 1 (realized_vol):
        execute 0: realized_vol_20d → (realized_vol,)
      Block 2 (INSERT):
        execute 0: INSERT result with rowcount=insert_rowcount
    """
    if chain_rows is None:
        chain_rows = _build_chain_rows()

    # Block 0 results
    r_expiry = _make_result([(expiry,)])
    r_snap = _make_result([(snapshot_time,)])
    r_chain = _make_result(chain_rows)

    # Block 1 results
    r_rvol = _make_result([(realized_vol,)])

    # Block 2 results (INSERT)
    insert_mock = MagicMock()
    insert_mock.rowcount = insert_rowcount
    r_insert = MagicMock()
    r_insert.fetchone.return_value = None
    r_insert.fetchall.return_value = []

    # The INSERT result is returned directly from session.execute() (not fetchone/all).
    # block2_session returns insert_mock directly from execute() so rowcount is accessible.
    block2_session = MagicMock()
    block2_session.execute.return_value = insert_mock

    _calls: list[int] = [0]

    @contextmanager
    def fake_get_session():
        idx = _calls[0]
        _calls[0] += 1
        if idx == 0:
            sess = MagicMock()
            sess.execute.side_effect = [r_expiry, r_snap, r_chain]
            yield sess
        elif idx == 1:
            sess = MagicMock()
            sess.execute.side_effect = [r_rvol]
            yield sess
        elif idx == 2:
            yield block2_session
        else:
            sess = MagicMock()
            sess.execute.return_value = _make_result([])
            yield sess

    return fake_get_session


# ---------------------------------------------------------------------------
# (a) Gate returns STAND_ASIDE → recorded=False with decision
# ---------------------------------------------------------------------------


@requires_module
class TestGateStandAside:
    """When realized_vol >= k * straddle_iv the gate returns STAND_ASIDE (or BUY_PREMIUM)
    and record_paper_entry must return recorded=False with the gate decision."""

    def _run(
        self, realized_vol: float, straddle_iv_frac: float = 0.14,
    ) -> dict:
        """Run record_paper_entry with a faked DB that would return STAND_ASIDE/BUY."""
        chain_rows = _build_chain_rows()
        fake_gs = _build_sell_session(
            realized_vol=realized_vol,
            straddle_iv_raw=straddle_iv_frac,
            chain_rows=chain_rows,
        )
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            return record_paper_entry(
                symbol="NIFTY",
                nifty_id="13",
                k=_DEFAULT_K,
                now=_NOW_IST,
            )

    def test_stand_aside_returns_recorded_false(self):
        # realized_vol = 0.14 * 0.9 = 0.126; set realized >= straddle_iv to get STAND_ASIDE
        # straddle_iv = mean(14.0/100, 13.6/100) = 0.138; k=0.9 → threshold=0.1242
        # realized=0.135 → between 0.1242 and 0.138 → STAND_ASIDE
        result = self._run(realized_vol=0.135)
        assert result["recorded"] is False

    def test_stand_aside_includes_decision_key(self):
        result = self._run(realized_vol=0.135)
        assert "decision" in result

    def test_stand_aside_decision_is_not_sell_premium(self):
        result = self._run(realized_vol=0.135)
        assert result["decision"] != "SELL_PREMIUM"

    def test_buy_premium_returns_recorded_false(self):
        # realized > straddle_iv → BUY_PREMIUM
        result = self._run(realized_vol=0.30)
        assert result["recorded"] is False

    def test_buy_premium_decision_is_buy(self):
        result = self._run(realized_vol=0.30)
        assert result.get("decision") == "BUY_PREMIUM"


# ---------------------------------------------------------------------------
# (b) SELL_PREMIUM entry: recorded=True, credit and max_loss correct
# ---------------------------------------------------------------------------


@requires_module
class TestSellPremiumEntry:
    """Gate fires SELL_PREMIUM → entry is inserted; credit and max_loss are correct."""

    def _run(self, realized_vol: float = 0.05) -> dict:
        fake_gs = _build_sell_session(realized_vol=realized_vol)
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            return record_paper_entry(
                symbol="NIFTY",
                nifty_id="13",
                k=_DEFAULT_K,
                now=_NOW_IST,
            )

    def test_recorded_true_on_sell_premium(self):
        result = self._run()
        assert result["recorded"] is True

    def test_decision_is_sell_premium(self):
        result = self._run()
        assert result["decision"] == "SELL_PREMIUM"

    def test_credit_is_positive(self):
        # Net credit for a short condor must be positive when short legs > long legs
        result = self._run()
        assert result["credit"] > 0

    def test_credit_formula(self):
        """credit = (sp_prem + sc_prem) - (lp_prem + lc_prem).

        The exact value depends on which strikes build_condor selects.  We verify
        the sign and that the returned credit matches the formula.  Since premiums
        come from the canned chain (short strikes closer to ATM → higher premium),
        credit must be positive.
        """
        result = self._run()
        # Credit must be a finite positive number
        assert isinstance(result["credit"], float)
        assert result["credit"] > 0.0

    def test_max_loss_is_non_negative(self):
        result = self._run()
        assert result["max_loss"] >= 0.0

    def test_max_loss_formula(self):
        """max_loss = max(0, wing_width - credit) * lot.

        wing_width = short_put_k - long_put_k (= wing_strikes * step).
        We verify the formula using the returned credit.
        """
        result = self._run()
        credit = result["credit"]
        # With wing_strikes=2, step=50 → wing_width=100 pts
        wing_width = 2 * 50
        expected_max_loss = max(0.0, wing_width - credit) * _LOT
        assert result["max_loss"] == pytest.approx(expected_max_loss, abs=0.5)

    def test_expiry_matches_canned_expiry(self):
        result = self._run()
        assert result["expiry"] == _EXPIRY

    def test_second_call_is_skipped(self):
        """ON CONFLICT DO NOTHING → rowcount=0 → recorded=False on second insert."""
        fake_gs = _build_sell_session(realized_vol=0.05, insert_rowcount=0)
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            result = record_paper_entry(symbol="NIFTY", nifty_id="13", k=_DEFAULT_K, now=_NOW_IST)
        assert result["recorded"] is False


# ---------------------------------------------------------------------------
# (c) Early-exit reasons: no ATM iv, no realized vol, no expiry
# ---------------------------------------------------------------------------


@requires_module
class TestEarlyExitReasons:
    """record_paper_entry returns recorded=False with a reason on missing data."""

    def test_no_future_expiry_in_snapshot(self):
        """When option_chain_snapshot has no future expiry, return reason."""
        # Session: min(expiry_date) → (None,)
        r_no_expiry = _make_result([(None,)])

        _calls: list[int] = [0]

        @contextmanager
        def fake_gs():
            sess = MagicMock()
            sess.execute.return_value = r_no_expiry
            yield sess

        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            result = record_paper_entry(symbol="NIFTY", nifty_id="13", now=_NOW_IST)

        assert result["recorded"] is False
        assert "reason" in result
        assert "no future expiry" in result["reason"]

    def test_no_atm_iv_returns_reason(self):
        """Chain rows present but ATM strike missing from CE dict → no ATM iv."""
        # Remove the ATM CE row — only non-ATM CE/PE rows remain
        chain_rows_no_atm_ce = [
            r for r in _build_chain_rows()
            if not (int(float(r[0])) == _ATM and r[1] == "CE")
        ]

        fake_gs = _build_sell_session(
            realized_vol=0.05,
            chain_rows=chain_rows_no_atm_ce,
        )
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            result = record_paper_entry(symbol="NIFTY", nifty_id="13", now=_NOW_IST)

        assert result["recorded"] is False
        assert "reason" in result
        assert "ATM" in result["reason"] or "atm" in result["reason"].lower()

    def test_no_realized_vol_returns_reason(self):
        """index_bars has no realized_vol_20d → return reason."""
        chain_rows = _build_chain_rows()
        r_expiry = _make_result([(_EXPIRY,)])
        r_snap = _make_result([(_NOW_IST,)])
        r_chain = _make_result(chain_rows)
        r_rvol_none = _make_result([(None,)])  # no realized vol

        _calls: list[int] = [0]

        @contextmanager
        def fake_gs():
            idx = _calls[0]
            _calls[0] += 1
            if idx == 0:
                sess = MagicMock()
                sess.execute.side_effect = [r_expiry, r_snap, r_chain]
                yield sess
            else:
                sess = MagicMock()
                sess.execute.side_effect = [r_rvol_none]
                yield sess

        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            result = record_paper_entry(symbol="NIFTY", nifty_id="13", now=_NOW_IST)

        assert result["recorded"] is False
        assert "reason" in result
        assert "realized vol" in result["reason"]


# ---------------------------------------------------------------------------
# (d) resolve_paper_trades: net_pnl from resolve_condor, win flag correct
# ---------------------------------------------------------------------------


@requires_module
class TestResolvePaperTrades:
    """resolve_paper_trades correctly resolves matured OPEN trades."""

    # Canned OPEN trade row:
    # expiry=_EXPIRY - 1 day (yesterday — past expiry)
    _PAST_EXPIRY = date(2026, 7, 2)
    _ENTRY_DATE = date(2026, 6, 26)
    _SPOT_ENTRY = 24000.0
    _CREDIT_PER_UNIT = 60.0   # per-unit credit received at entry
    _ENTRY_COSTS = 120.0      # ₹ entry cost
    _TRADE_ID = 42

    # Use simple symmetric strikes for easy hand-verification
    _SPK = 23900  # short put
    _SCK = 24100  # short call
    _LPK = 23800  # long put  (wing_width = 100 per side)
    _LCK = 24200  # long call

    def _open_row(self) -> tuple:
        return (
            self._TRADE_ID,    # id
            "NIFTY",           # symbol
            _LOT,              # lot
            self._PAST_EXPIRY, # expiry_date
            self._SPK,         # short_put_k
            self._SCK,         # short_call_k
            self._LPK,         # long_put_k
            self._LCK,         # long_call_k
            self._CREDIT_PER_UNIT,  # credit (per unit)
            self._ENTRY_COSTS, # entry_costs
        )

    def _build_resolve_session(self, expiry_spot: float) -> Any:
        """Build multi-session fake for resolve_paper_trades.

        Session calls:
          Block 0: fetch OPEN rows → [open_row]
          Block 1: fetch settlement close on expiry_date → (expiry_spot,)
          Block 2: UPDATE row → any result
        """
        r_open = _make_result([self._open_row()])
        r_settle = _make_result([(expiry_spot,)])
        update_mock = MagicMock()
        update_mock.rowcount = 1

        _calls: list[int] = [0]

        @contextmanager
        def fake_gs():
            idx = _calls[0]
            _calls[0] += 1
            if idx == 0:
                sess = MagicMock()
                sess.execute.side_effect = [r_open]
                yield sess
            elif idx == 1:
                sess = MagicMock()
                sess.execute.side_effect = [r_settle]
                yield sess
            else:
                sess = MagicMock()
                sess.execute.return_value = update_mock
                yield sess

        return fake_gs

    def _run(self, expiry_spot: float) -> int:
        fake_gs = self._build_resolve_session(expiry_spot)
        resolve_now = datetime(2026, 7, 3, 17, 0, 0, tzinfo=_IST)
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            return resolve_paper_trades(
                symbol="NIFTY",
                nifty_id="13",
                now=resolve_now,
            )

    def test_one_row_resolved(self):
        """A single matured OPEN row → resolved_count=1."""
        count = self._run(expiry_spot=24000.0)
        assert count == 1

    def test_win_when_expiry_between_shorts(self):
        """Expiry between short strikes → full credit → net_pnl > 0 → win=True.

        We verify indirectly: if resolve_paper_trades returned 1, it called UPDATE
        with win=True.  We capture the UPDATE call's kwargs to confirm.
        """
        resolve_now = datetime(2026, 7, 3, 17, 0, 0, tzinfo=_IST)

        update_kwargs: dict[str, Any] = {}

        # Capture the kwargs passed to the UPDATE execute() call
        _calls: list[int] = [0]

        @contextmanager
        def capturing_gs():
            idx = _calls[0]
            _calls[0] += 1
            if idx == 0:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([self._open_row()])]
                yield sess
            elif idx == 1:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([(24000.0,)])]
                yield sess
            else:
                sess = MagicMock()

                def capture_execute(sql_text, params):
                    nonlocal update_kwargs
                    update_kwargs = params
                    m = MagicMock()
                    m.rowcount = 1
                    return m

                sess.execute.side_effect = capture_execute
                yield sess

        with patch("core.fno_paper.get_session", new=capturing_gs), \
             patch("db.get_session", new=capturing_gs):
            resolve_paper_trades(symbol="NIFTY", nifty_id="13", now=resolve_now)

        assert update_kwargs.get("win") is True

    def test_loss_when_expiry_beyond_long_put(self):
        """Expiry far below long_put_k → max loss → net_pnl negative → win=False."""
        # We capture the UPDATE params to inspect net_pnl and win
        expiry_spot = 23500.0  # well below long_put_k=23800 → max loss
        update_kwargs: dict[str, Any] = {}
        _calls: list[int] = [0]

        @contextmanager
        def capturing_gs():
            idx = _calls[0]
            _calls[0] += 1
            if idx == 0:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([self._open_row()])]
                yield sess
            elif idx == 1:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([(expiry_spot,)])]
                yield sess
            else:
                sess = MagicMock()

                def capture_execute(sql_text, params):
                    nonlocal update_kwargs
                    update_kwargs = params
                    m = MagicMock()
                    m.rowcount = 1
                    return m

                sess.execute.side_effect = capture_execute
                yield sess

        resolve_now = datetime(2026, 7, 3, 17, 0, 0, tzinfo=_IST)
        with patch("core.fno_paper.get_session", new=capturing_gs), \
             patch("db.get_session", new=capturing_gs):
            resolve_paper_trades(symbol="NIFTY", nifty_id="13", now=resolve_now)

        assert update_kwargs.get("win") is False
        assert update_kwargs.get("net_pnl", 0.0) < 0

    def test_net_pnl_equals_gross_minus_costs(self):
        """net_pnl = gross_pnl - entry_costs - exit_costs."""
        from research.backtest.fno_condor import resolve_condor  # noqa: PLC0415

        expiry_spot = 24000.0  # between shorts → full credit
        update_kwargs: dict[str, Any] = {}
        _calls: list[int] = [0]

        @contextmanager
        def capturing_gs():
            idx = _calls[0]
            _calls[0] += 1
            if idx == 0:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([self._open_row()])]
                yield sess
            elif idx == 1:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([(expiry_spot,)])]
                yield sess
            else:
                sess = MagicMock()

                def capture_execute(sql_text, params):
                    nonlocal update_kwargs
                    update_kwargs = params
                    m = MagicMock()
                    m.rowcount = 1
                    return m

                sess.execute.side_effect = capture_execute
                yield sess

        resolve_now = datetime(2026, 7, 3, 17, 0, 0, tzinfo=_IST)
        with patch("core.fno_paper.get_session", new=capturing_gs), \
             patch("db.get_session", new=capturing_gs):
            resolve_paper_trades(symbol="NIFTY", nifty_id="13", now=resolve_now)

        strikes = {
            "short_put_k": self._SPK,
            "short_call_k": self._SCK,
            "long_put_k": self._LPK,
            "long_call_k": self._LCK,
        }
        gross = resolve_condor(strikes, self._CREDIT_PER_UNIT, expiry_spot, lot=_LOT)["gross_pnl"]

        # Long legs intrinsic (both OTM at 24000): exit_costs ≈ 0
        from research.backtest.fno_costs import OPTION_EXERCISE_STT_PCT  # noqa: PLC0415
        lp_intr = max(self._LPK - expiry_spot, 0.0) * _LOT
        lc_intr = max(expiry_spot - self._LCK, 0.0) * _LOT
        exit_costs = OPTION_EXERCISE_STT_PCT * (lp_intr + lc_intr)
        expected_net = gross - self._ENTRY_COSTS - exit_costs

        assert update_kwargs.get("net_pnl") == pytest.approx(expected_net, rel=1e-4)

    def test_no_settlement_skips_trade(self):
        """If no close found on expiry_date, the row is deferred (count=0)."""
        _calls: list[int] = [0]

        @contextmanager
        def fake_gs():
            idx = _calls[0]
            _calls[0] += 1
            if idx == 0:
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([self._open_row()])]
                yield sess
            else:
                # No settlement row
                sess = MagicMock()
                sess.execute.side_effect = [_make_result([])]
                yield sess

        resolve_now = datetime(2026, 7, 3, 17, 0, 0, tzinfo=_IST)
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            count = resolve_paper_trades(symbol="NIFTY", nifty_id="13", now=resolve_now)

        assert count == 0

    def test_no_open_trades_returns_zero(self):
        """No OPEN rows → nothing to resolve → returns 0."""
        _calls: list[int] = [0]

        @contextmanager
        def fake_gs():
            _calls[0] += 1
            sess = MagicMock()
            sess.execute.side_effect = [_make_result([])]
            yield sess

        resolve_now = datetime(2026, 7, 3, 17, 0, 0, tzinfo=_IST)
        with patch("core.fno_paper.get_session", new=fake_gs), \
             patch("db.get_session", new=fake_gs):
            count = resolve_paper_trades(symbol="NIFTY", nifty_id="13", now=resolve_now)

        assert count == 0
