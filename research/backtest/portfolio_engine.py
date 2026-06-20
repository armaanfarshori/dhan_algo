"""
Portfolio-level replay — the realizable backtest.

The per-security `engine.replay_security_day` gives every name its OWN
RiskEngine(equity_base=params.equity) and the caller sums P&L. That is
unrealizable: it assumes infinite capital and that you took every signal across
~1,700 names simultaneously.

This module mirrors the LIVE architecture instead (apps/trader.py: N runners
share ONE RiskEngine + ONE portfolio). All universe names for a day are replayed
against a SINGLE shared book, so entries compete for finite capital, the
concurrent-position cap applies, and a portfolio-wide daily-loss kill-switch can
halt the day. Equity compounds across days exactly as live.

Correctness bones preserved from engine.py:
  • same `strategies/orb.py` class as live;
  • no look-ahead — a decision on bar i fills at bar i+1's OPEN + adverse
    slippage (last bar fills at close);
  • full Indian cost stack via `round_trip_costs`;
  • sizing/risk via the live `engine.risk.RiskEngine` math (no drift).

Reuses RiskEngine for the MATH (size_position, position_risk, register/release
risk, daily_loss_budget, equity compounding) but keeps the book IN-MEMORY — a
2y×1700 run through Portfolio.apply_fill would be millions of DB writes.
"""
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from research.backtest.costs import round_trip_costs
from research.backtest.engine import BTTrade, _slip, load_day_bars, load_prior_daily
from research.backtest.registry import STRATEGIES
from strategies.orb import ORB, ORBParams, Decision

logger = logging.getLogger("dhan.backtest.portfolio")

IST = ZoneInfo("Asia/Kolkata")

# How many prior daily-volume observations to average for ADV (mirrors live ~20 trading days).
_ADV_WINDOW = 20


@dataclass
class PortfolioParams:
    equity: float = 500_000.0
    risk_per_trade: float = 0.005       # live default (engine/risk.py RiskParams)
    max_notional_pct: float = 0.20
    slippage_bps: float = 2.0
    # Portfolio-level caps (mirror config.py live values).
    max_open_positions: int = 10           # concurrent positions across all names
    max_orders_per_session: int = 4        # entries per security per session
    max_daily_loss_pct: float = 0.02       # portfolio daily-loss kill-switch
    max_weekly_loss_pct: float = 0.05      # portfolio weekly-loss kill-switch (mirrors live RiskParams)
    min_stop_distance_pct: float = 0.0035
    adv_participation_pct: float = 0.01
    tick_size: float = 0.05                # NSE tick; slippage floored at half-tick
    partial_fill_pct: float = 0.10         # fill qty capped at this % of the fill-bar volume
    orb: ORBParams = field(default_factory=ORBParams)
    # Strategy to use; must be a key in research.backtest.registry.STRATEGIES.
    # Defaults to "orb" so existing callers that never set this field are unchanged.
    strategy_name: str = "orb"


class _Book:
    """In-memory shared portfolio. Realized P&L feeds the RiskEngine's equity so
    sizing compounds (and shrinks in drawdown) just like live."""

    def __init__(self, start_equity: float, risk: RiskEngine):
        self.start_equity = start_equity
        self.realized_total = 0.0          # all-time (across days) — drives equity
        self.realized_today = 0.0
        self.realized_week = 0.0           # rolling 5-session week (mirrors live _realized_week)
        self.positions: dict[str, dict] = {}   # sid -> {side,qty,entry_px,entry_ts,stop,target}
        self.risk = risk
        self.halted_today = False

    @property
    def equity(self) -> float:
        return max(1.0, self.start_equity + self.realized_total)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def _sync_risk(self):
        # The RiskEngine derives equity + budgets from _realized_total; keep it in
        # lockstep with the in-memory book (no DB refresh_pnl in backtest).
        self.risk._realized_total = self.realized_total
        self.risk._realized_today = self.realized_today
        self.risk._realized_week = self.realized_week

    def unrealized_pnl(self, ltp: dict[str, float]) -> float:
        """Mark open positions to current prices (mirrors live RiskEngine._evaluate)."""
        total = 0.0
        for sid, pos in self.positions.items():
            px = ltp.get(sid, pos["entry_px"])
            direction = 1 if pos["side"] == "LONG" else -1
            total += (px - pos["entry_px"]) * pos["qty"] * direction
        return total


def _close(book: _Book, trades: list, sid: str, day: date, raw_px: float,
           exit_ts: datetime, reason: str, slippage_bps: float, orb: ORB,
           tick: float = 0.0):
    """Close sid's position at raw_px (a bar open or close) + adverse slippage,
    book the full-cost round trip, and release its risk budget."""
    pos = book.positions.pop(sid)
    exit_side = "SELL" if pos["side"] == "LONG" else "BUY"
    fill_px = _slip(raw_px, exit_side, slippage_bps, tick)
    direction = 1 if pos["side"] == "LONG" else -1
    gross = (fill_px - pos["entry_px"]) * pos["qty"] * direction
    buy_px, sell_px = ((pos["entry_px"], fill_px) if direction == 1
                       else (fill_px, pos["entry_px"]))
    costs = round_trip_costs(buy_px, sell_px, pos["qty"]).total
    net = gross - costs
    trades.append(BTTrade(
        security_id=sid, day=day, side=pos["side"], qty=pos["qty"],
        entry_ts=pos["entry_ts"], entry_price=pos["entry_px"],
        exit_ts=exit_ts, exit_price=fill_px, exit_reason=reason,
        gross_pnl=round(gross, 2), costs=costs, net_pnl=round(net, 2)))
    book.realized_total += net
    book.realized_today += net
    book.realized_week += net
    book.risk.release_risk(sid)
    orb.notify_flat()


def _check_intrabar_exit(book: _Book, trades: list, sid: str, day: date,
                         bar, ts: datetime, params: PortfolioParams,
                         orb: ORB) -> bool:
    """Detect intrabar stop/target wick hits for an open position in sid.

    Mirrors the live fill model: the engine monitors the live price stream and
    can exit mid-bar when price touches a stop or target. In a bar-by-bar
    replay we only see OHLC, so we test bar.low/high against the stop/target
    levels and fill AT the stop/target price (stop-first when both are hit).

    Stop levels (from orb.py):
      LONG:  stop  = or_low  * (1 - sl_buffer_pct)
             target = entry  + multiplier * or_range
      SHORT: stop  = or_high * (1 + sl_buffer_pct)
             target = entry  - multiplier * or_range

    Returns True if the position was closed (caller skips on_tick exit logic)."""
    if sid not in book.positions:
        return False
    pos = book.positions[sid]
    stop = pos["stop"]
    target = pos["target"]
    bar_low = float(bar["low"])
    bar_high = float(bar["high"])

    if pos["side"] == "LONG":
        stop_hit = bar_low <= stop
        target_hit = bar_high >= target
        if stop_hit or target_hit:
            # Stop-first: if the wick triggered both, the adverse outcome wins.
            if stop_hit:
                # Gap-aware: a bar that gapped through the stop (open below it)
                # fills at the open — you can't exit a gap-down at the stop. Take
                # the WORSE of stop vs gap-open so gap losses aren't understated.
                fill_px = min(float(bar["open"]), stop)
                reason = f"Stop-loss ₹{fill_px:.2f} (intrabar)"
            else:
                fill_px = target
                reason = f"Target hit ₹{target:.2f} (intrabar)"
            _close(book, trades, sid, day, fill_px, ts, reason,
                   params.slippage_bps, orb, params.tick_size)
            return True
    else:  # SHORT
        stop_hit = bar_high >= stop
        target_hit = bar_low <= target
        if stop_hit or target_hit:
            if stop_hit:
                # Gap-aware (SHORT): a gap-up through the stop fills at the open.
                fill_px = max(float(bar["open"]), stop)
                reason = f"Stop-loss ₹{fill_px:.2f} (intrabar)"
            else:
                fill_px = target
                reason = f"Target hit ₹{target:.2f} (intrabar)"
            _close(book, trades, sid, day, fill_px, ts, reason,
                   params.slippage_bps, orb, params.tick_size)
            return True
    return False


async def replay_portfolio(universe_by_day: dict[date, list[str]],
                           params: PortfolioParams, gate_fn=None):
    """Replay all days against one shared book. Returns (trades, daily_records)
    where daily_records = [{day, net_pnl, equity_end, kill_switch}]."""
    rp = RiskParams(
        equity_base=params.equity, risk_per_trade=params.risk_per_trade,
        max_notional_pct=params.max_notional_pct,
        max_daily_loss_pct=params.max_daily_loss_pct,
        weekly_loss_pct=params.max_weekly_loss_pct,
        min_stop_distance_pct=params.min_stop_distance_pct,
        adv_participation_pct=params.adv_participation_pct,
        max_open_positions=params.max_open_positions)
    risk = RiskEngine(rp, Portfolio(mode="BACKTEST"), ltp_lookup=lambda _s: 0.0)
    book = _Book(params.equity, risk)
    trades: list[BTTrade] = []
    daily: list[dict] = []

    # Rolling per-security daily volume history for ADV — no look-ahead:
    # only volumes from PRIOR completed sessions are used to size today's trades.
    # deque(maxlen=_ADV_WINDOW) keeps the most recent _ADV_WINDOW observations.
    adv_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_ADV_WINDOW))
    _current_iso_week: tuple[int, int] | None = None  # (iso_year, iso_week)

    for day in sorted(universe_by_day):
        book.realized_today = 0.0
        book.halted_today = False
        # Reset the weekly realized bucket at the start of each ISO week (mirrors live
        # refresh_pnl which sums from date_trunc('week', ...) each monitor cycle).
        iso_year, iso_week, _ = day.isocalendar()
        if (iso_year, iso_week) != _current_iso_week:
            book.realized_week = 0.0
            _current_iso_week = (iso_year, iso_week)

        # Load each name's session; skip empties / too-short sessions.
        bars = {}
        for sid in universe_by_day[day]:
            df = load_day_bars(sid, day)
            if not df.empty and len(df) >= params.orb.orb_minutes + 3:
                bars[sid] = df.reset_index(drop=True)
        if not bars:
            daily.append({"day": str(day), "net_pnl": 0.0,
                          "equity_end": round(book.equity, 2), "kill_switch": False})
            continue

        # Snapshot ADV from prior-session history BEFORE today's bars are observed.
        # adv_history was updated at EOD of each prior session; read it now (no
        # look-ahead). None → liquidity cap not applied (same as live get_adv failure).
        adv_snapshot: dict[str, float | None] = {}
        for sid in bars:
            hist = adv_history[sid]
            adv_snapshot[sid] = (sum(hist) / len(hist)) if hist else None

        strategy_cls, params_cls = STRATEGIES.get(params.strategy_name, (ORB, ORBParams))
        if params.strategy_name == "orb":
            orbs = {sid: strategy_cls(sid, params.orb) for sid in bars}
        else:
            orbs = {sid: strategy_cls(sid, params_cls()) for sid in bars}

        # Prior-day seed — additive hook for strategies that expose seed_prior_day.
        # ORB does not expose this method so this loop is a no-op for ORB runs.
        # Degrade gracefully (skip the call) when no prior daily bar exists.
        _sample_strat = next(iter(orbs.values())) if orbs else None
        if _sample_strat is not None and hasattr(_sample_strat, "seed_prior_day"):
            for sid, strat in orbs.items():
                prior = load_prior_daily(sid, day)
                if prior is not None:
                    p_high, p_low, p_close = prior
                    strat.seed_prior_day(p_high, p_low, p_close)

        entries_today = {sid: 0 for sid in bars}
        pending: dict[str, tuple] = {}
        last_idx = {sid: len(df) - 1 for sid, df in bars.items()}

        # Time-merged event stream: (timestamp, sid, bar_index), stable by sid.
        events = [(bars[sid].iloc[i]["time"], sid, i)
                  for sid in bars for i in range(len(bars[sid]))]
        events.sort(key=lambda e: (e[0], e[1]))

        for _ts, sid, idx in events:
            df = bars[sid]
            bar = df.iloc[idx]
            ts = bar["time"].to_pydatetime()
            orb = orbs[sid]

            # 1. Execute the decision queued on this name's PREVIOUS bar, at THIS
            #    bar's open (no look-ahead).
            if sid in pending:
                kind, d, _ignored_qty = pending.pop(sid)
                if kind == "ENTER" and not book.halted_today and book.open_count < params.max_open_positions:
                    fill_px = _slip(float(bar["open"]), d.side,
                                    params.slippage_bps, params.tick_size)
                    # FIX (P1-sizing): compute qty from the ACTUAL fill price (this
                    # bar's slipped open), not the signal-bar close queued earlier.
                    # A gap open can shift the price materially; sizing here prevents
                    # over- or under-leverage (mirrors engine.py "FIX (P0-sizing)").
                    # ADV cap and risk-budget check use the real fill price too.
                    book._sync_risk()
                    qty = risk.size_position(entry=fill_px, stop=d.stop,
                                             adv=adv_snapshot.get(sid))
                    # Partial fill: you can't take unlimited size out of one bar of
                    # a thin name — cap qty at a % of the fill bar's volume.
                    if params.partial_fill_pct > 0:
                        cap = int(params.partial_fill_pct * float(bar["volume"]))
                        qty = min(qty, cap)
                    if qty > 0:
                        # Re-check daily risk budget at the real fill price — a gap
                        # can increase the notional risk vs the preliminary check.
                        new_risk = risk.position_risk(fill_px, d.stop, qty)
                        consumed = risk.committed_risk + max(0.0, -book.realized_today)
                        if consumed + new_risk > risk.daily_loss_budget:
                            qty = 0  # budget exhausted at fill price — skip
                    if qty > 0:
                        # Use the stop and target captured from the ENTER Decision
                        # (strategy-agnostic — no ORB-internal field access needed).
                        # The strategy is contractually required to set concrete
                        # absolute stop/target on every ENTER Decision.
                        book.positions[sid] = {
                            "side": "LONG" if d.side == "BUY" else "SHORT",
                            "qty": qty, "entry_px": fill_px, "entry_ts": ts,
                            "stop": d.stop, "target": d.target}
                        risk.register_risk(sid, risk.position_risk(fill_px, d.stop, qty))
                        orb.notify_fill(d.side, qty, fill_px)
                        # Count the entry only when it ACTUALLY fills — a dropped/
                        # zero-volume fill must not burn a per-session entry slot.
                        entries_today[sid] += 1
                elif kind == "EXIT" and sid in book.positions:
                    _close(book, trades, sid, day, float(bar["open"]), ts, d.reason,
                           params.slippage_bps, orb, params.tick_size)

            # 1b. Intrabar stop/target wick check (P0 fix: stop/target hit detection).
            # Before calling on_tick (which evaluates only close price), test whether
            # this bar's wick pierced the position's stop or target. Fill at the
            # stop/target price with adverse slippage; stop-first if both triggered.
            if sid in book.positions:
                intrabar_exited = _check_intrabar_exit(
                    book, trades, sid, day, bar, ts, params, orb)
                if intrabar_exited:
                    # Position closed; on_tick exit logic below is not needed.
                    continue

            # 2. Portfolio daily/weekly loss kill-switch (mirrors live engine/_evaluate).
            # Live engine/_evaluate() computes day_total = realized + unrealized and
            # week_total = realized_week + unrealized, then halts on either breach.
            # Mirror that: mark open positions to the current bar's close price.
            book._sync_risk()
            if not book.halted_today:
                # Build a per-sid ltp from each open position's most recent bar close.
                # For sids whose bar stream is behind the current timestamp we use the
                # last bar close up to this moment — no look-ahead.
                ltp_now: dict[str, float] = {}
                for osid in list(book.positions):
                    if osid in bars:
                        osid_df = bars[osid]
                        # Find the latest bar index for osid whose time <= current ts
                        osid_idx = osid_df["time"].searchsorted(bar["time"], side="right") - 1
                        if osid_idx >= 0:
                            ltp_now[osid] = float(osid_df.iloc[osid_idx]["close"])
                        else:
                            ltp_now[osid] = book.positions[osid]["entry_px"]
                    else:
                        ltp_now[osid] = book.positions[osid]["entry_px"]

                unrealized = book.unrealized_pnl(ltp_now)
                day_total = book.realized_today + unrealized
                week_total = book.realized_week + unrealized
                halt_reason: str | None = None
                if day_total <= -risk.daily_loss_budget:
                    halt_reason = "portfolio kill-switch (daily)"
                    logger.info(
                        "[%s] portfolio daily kill-switch: realized %.0f + unrealized %.0f"
                        " = %.0f <= -budget %.0f",
                        day, book.realized_today, unrealized, day_total,
                        risk.daily_loss_budget)
                elif week_total <= -risk.weekly_loss_budget:
                    halt_reason = "portfolio kill-switch (weekly)"
                    logger.info(
                        "[%s] portfolio weekly kill-switch: week_realized %.0f + unrealized %.0f"
                        " = %.0f <= -budget %.0f",
                        day, book.realized_week, unrealized, week_total,
                        risk.weekly_loss_budget)
                if halt_reason is not None:
                    book.halted_today = True
                    for osid in list(book.positions):
                        pending.setdefault(osid, ("EXIT",
                            Decision(action="EXIT", reason=halt_reason), 0))

            # 3. Strategy sees the bar (pure ORB).
            decision = orb.on_tick(ts, float(bar["close"]),
                                   high=float(bar["high"]), low=float(bar["low"]),
                                   volume=float(bar["volume"]))
            if decision is None:
                continue

            if decision.action == "ENTER" and sid not in book.positions and not book.halted_today:
                if entries_today[sid] >= params.max_orders_per_session:
                    continue
                if book.open_count >= params.max_open_positions:
                    continue
                if idx == last_idx[sid]:
                    continue                       # no next bar to fill on
                if gate_fn is not None:
                    if not await gate_fn(sid, decision.side, df.iloc[: idx + 1]):
                        continue
                # Qty is deferred to fill time (next bar's open) so it is sized
                # from the ACTUAL fill price, not the signal-bar close.  A gap open
                # can shift the price materially; deferring prevents over-leverage
                # (mirrors engine.py — see "FIX (P0-sizing)" comment there).
                # We do a PRELIMINARY risk check here using the signal-bar close so
                # obviously budget-busting trades are rejected cheaply; the fill-time
                # check re-evaluates with the real fill price and qty.
                book._sync_risk()
                signal_close = float(bar["close"])
                pre_qty = risk.size_position(entry=signal_close, stop=decision.stop,
                                             adv=adv_snapshot.get(sid))
                if pre_qty <= 0:
                    continue
                new_risk = risk.position_risk(signal_close, decision.stop, pre_qty)
                # Match live RiskEngine.check_intent: realized losses already
                # booked today ALSO consume the daily budget (the backtest was
                # optimistic vs live in drawn-down sessions without this term).
                consumed = risk.committed_risk + max(0.0, -book.realized_today)
                if consumed + new_risk > risk.daily_loss_budget:
                    continue                       # would exceed daily risk budget
                # Store qty=0 sentinel; fill time recomputes from actual fill price.
                pending[sid] = ("ENTER", decision, 0)

            elif decision.action == "EXIT" and sid in book.positions:
                if idx == last_idx[sid]:
                    _close(book, trades, sid, day, float(bar["close"]), ts,
                           decision.reason, params.slippage_bps, orb, params.tick_size)
                else:
                    pending[sid] = ("EXIT", decision, 0)

        # EOD: force-close anything still open at its last bar's close.
        # P0 fix (bug 3): a position whose security has NO bars today is force-closed
        # at the last known price (prior session's close), booking real P&L + a trade
        # record. Prior code silently dropped these at zero P&L, understating losses.
        for sid in list(book.positions):
            df = bars.get(sid)
            orb_inst = orbs.get(sid)
            if df is None or df.empty or orb_inst is None:
                # Leaked position: security suspended/halted/left universe.
                # Close at last known price (entry price is the only safe fallback
                # if we have no bars at all; in practice the prior EOD would have
                # set entry_px to a realistic price).
                pos = book.positions[sid]
                last_px = pos["entry_px"]   # worst-case fallback (no prior bars loaded)
                logger.warning(
                    "[%s] EOD: %s has no bars today — force-closing at last known px %.2f",
                    day, sid, last_px)
                # Flatten the REAL strategy instance so non-ORB session state is
                # cleared via its own notify_flat(); fall back to a stub ORB only
                # if (somehow) no instance exists for this sid.
                inst = orbs.get(sid)
                if inst is None:
                    inst = ORB(sid, params.orb)
                    inst.position = pos["qty"] if pos["side"] == "LONG" else -pos["qty"]
                _close(book, trades, sid, day, last_px,
                       datetime.combine(day, dt_time(15, 30), tzinfo=IST),
                       "forced close — no bars (halt/suspension)",
                       params.slippage_bps, inst, params.tick_size)
                continue
            last = df.iloc[-1]
            _close(book, trades, sid, day, float(last["close"]),
                   last["time"].to_pydatetime(), "forced EOD close",
                   params.slippage_bps, orb_inst, params.tick_size)

        # Update ADV history AFTER all today's bars are consumed — no look-ahead.
        # Sum the 1-min bar volumes for each security to get the session volume,
        # then push it into the rolling window for the next day's sizing.
        for sid, df in bars.items():
            session_vol = float(df["volume"].sum())
            adv_history[sid].append(session_vol)

        daily.append({"day": str(day), "net_pnl": round(book.realized_today, 2),
                      "equity_end": round(book.equity, 2),
                      "kill_switch": book.halted_today})

    return trades, daily
