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
from dataclasses import dataclass, field
from datetime import date, datetime

from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from research.backtest.costs import round_trip_costs
from research.backtest.engine import BTTrade, _slip, load_day_bars
from strategies.orb import ORB, ORBParams, Decision

logger = logging.getLogger("dhan.backtest.portfolio")


@dataclass
class PortfolioParams:
    equity: float = 500_000.0
    risk_per_trade: float = 0.01
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
        self.positions: dict[str, dict] = {}   # sid -> {side,qty,entry_px,entry_ts,stop}
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
                        book.positions[sid] = {
                            "side": "LONG" if d.side == "BUY" else "SHORT",
                            "qty": qty, "entry_px": fill_px, "entry_ts": ts, "stop": d.stop}
                        risk.register_risk(sid, risk.position_risk(fill_px, d.stop, qty))
                        orb.notify_fill(d.side, qty, fill_px)
                        # Count the entry only when it ACTUALLY fills — a dropped/
                        # zero-volume fill must not burn a per-session entry slot.
                        entries_today[sid] += 1
                elif kind == "EXIT" and sid in book.positions:
                    _close(book, trades, sid, day, float(bar["open"]), ts, d.reason,
                           params.slippage_bps, orb, params.tick_size)

            # 2. Portfolio daily-loss kill-switch: queue exits for all open names,
            #    block new entries for the rest of the day.
            book._sync_risk()
            if not book.halted_today and book.realized_today <= -risk.daily_loss_budget:
                book.halted_today = True
                logger.info("[%s] portfolio kill-switch: realized %.0f <= -budget %.0f",
                            day, book.realized_today, risk.daily_loss_budget)
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
                qty = risk.size_position(entry=float(bar["close"]), stop=decision.stop)
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

        # EOD: force-close anything still open at its last bar's close. Guard
        # against a position whose bars aren't in today's set (only possible if a
        # prior day aborted mid-replay and leaked a position) — drop it safely
        # rather than KeyError-cascade.
        for sid in list(book.positions):
            df = bars.get(sid)
            if df is None or df.empty or sid not in orbs:
                logger.warning("[%s] EOD: leaked position %s has no bars today — dropping", day, sid)
                book.positions.pop(sid, None)
                risk.release_risk(sid)
                continue
            last = df.iloc[-1]
            _close(book, trades, sid, day, float(last["close"]),
                   last["time"].to_pydatetime(), "forced EOD close",
                   params.slippage_bps, orbs[sid], params.tick_size)

        daily.append({"day": str(day), "net_pnl": round(book.realized_today, 2),
                      "equity_end": round(book.equity, 2),
                      "kill_switch": book.halted_today})

    return trades, daily
