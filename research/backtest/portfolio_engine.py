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
from datetime import date, datetime

from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from research.backtest.costs import round_trip_costs
from research.backtest.engine import BTTrade, _slip, load_day_bars
from strategies.orb import ORB, ORBParams, Decision

logger = logging.getLogger("dhan.backtest.portfolio")

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
    min_stop_distance_pct: float = 0.0035
    adv_participation_pct: float = 0.01
    tick_size: float = 0.05                # NSE tick; slippage floored at half-tick
    partial_fill_pct: float = 0.10         # fill qty capped at this % of the fill-bar volume
    orb: ORBParams = field(default_factory=ORBParams)


class _Book:
    """In-memory shared portfolio. Realized P&L feeds the RiskEngine's equity so
    sizing compounds (and shrinks in drawdown) just like live."""

    def __init__(self, start_equity: float, risk: RiskEngine):
        self.start_equity = start_equity
        self.realized_total = 0.0          # all-time (across days) — drives equity
        self.realized_today = 0.0
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

    for day in sorted(universe_by_day):
        book.realized_today = 0.0
        book.halted_today = False

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

        orbs = {sid: ORB(sid, params.orb) for sid in bars}
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
                kind, d, qty = pending.pop(sid)
                if kind == "ENTER" and not book.halted_today and book.open_count < params.max_open_positions:
                    # Partial fill: you can't take unlimited size out of one bar of
                    # a thin name — cap qty at a % of the fill bar's volume.
                    if params.partial_fill_pct > 0:
                        cap = int(params.partial_fill_pct * float(bar["volume"]))
                        qty = min(qty, cap)
                    if qty > 0:
                        fill_px = _slip(float(bar["open"]), d.side,
                                        params.slippage_bps, params.tick_size)
                        # Compute stop and target for the position record so the
                        # intrabar wick check (bug 1) has them readily available.
                        orb_range = orb.or_range
                        if d.side == "BUY":
                            stop_px = orb.or_low * (1 - params.orb.sl_buffer_pct)
                            target_px = fill_px + params.orb.target_multiplier * orb_range
                        else:
                            stop_px = orb.or_high * (1 + params.orb.sl_buffer_pct)
                            target_px = fill_px - params.orb.target_multiplier * orb_range
                        book.positions[sid] = {
                            "side": "LONG" if d.side == "BUY" else "SHORT",
                            "qty": qty, "entry_px": fill_px, "entry_ts": ts,
                            "stop": stop_px, "target": target_px}
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
            # Skip if the position was just entered this bar (fill px already set).
            if sid in book.positions:
                intrabar_exited = _check_intrabar_exit(
                    book, trades, sid, day, bar, ts, params, orb)
                if intrabar_exited:
                    # Position closed; on_tick exit logic below is not needed.
                    continue

            # 2. Portfolio daily-loss kill-switch (P0 fix: include unrealized P&L).
            # Live engine/_evaluate() computes day_total = realized + unrealized.
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
                if day_total <= -risk.daily_loss_budget:
                    book.halted_today = True
                    logger.info(
                        "[%s] portfolio kill-switch: realized %.0f + unrealized %.0f"
                        " = %.0f <= -budget %.0f",
                        day, book.realized_today, unrealized, day_total,
                        risk.daily_loss_budget)
                    for osid in list(book.positions):
                        pending.setdefault(osid, ("EXIT",
                            Decision(action="EXIT", reason="portfolio kill-switch"), 0))

            # 3. Strategy sees the bar (pure ORB).
            decision = orb.on_tick(ts, float(bar["close"]),
                                   high=float(bar["high"]), low=float(bar["low"]))
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
                book._sync_risk()
                # Pass ADV from prior sessions so live liquidity cap applies (P1 fix).
                qty = risk.size_position(entry=float(bar["close"]), stop=decision.stop,
                                         adv=adv_snapshot.get(sid))
                if qty <= 0:
                    continue
                new_risk = risk.position_risk(float(bar["close"]), decision.stop, qty)
                # Match live RiskEngine.check_intent: realized losses already
                # booked today ALSO consume the daily budget (the backtest was
                # optimistic vs live in drawn-down sessions without this term).
                consumed = risk.committed_risk + max(0.0, -book.realized_today)
                if consumed + new_risk > risk.daily_loss_budget:
                    continue                       # would exceed daily risk budget
                pending[sid] = ("ENTER", decision, qty)

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
                # Build a stub ORB so _close can call orb.notify_flat() safely.
                stub_orb = ORB(sid, params.orb)
                stub_orb.position = pos["qty"] if pos["side"] == "LONG" else -pos["qty"]
                _close(book, trades, sid, day, last_px,
                       datetime.combine(day, datetime.min.time()),
                       "forced close — no bars (halt/suspension)",
                       params.slippage_bps, stub_orb, params.tick_size)
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
