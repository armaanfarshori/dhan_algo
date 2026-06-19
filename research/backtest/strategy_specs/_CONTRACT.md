# Strategy integration contract (read this before planning/coding any strategy)

We are adding pluggable intraday strategies alongside the existing ORB, all backtested on the
SAME engine, costs, universe, and OOS split so results are directly comparable.

## The strategy interface (mirror `strategies/orb.py`)
Each strategy is a pure, synchronous, IO-free class in `strategies/<name>.py`:

```python
@dataclass
class <Name>Params:
    ...                       # all tunables with sane defaults

class <Name>:
    def __init__(self, security_id: str, params: Optional[<Name>Params] = None): ...
    def on_tick(self, now: datetime, price: float,
                high: Optional[float]=None, low: Optional[float]=None,
                volume: Optional[float]=None) -> Optional[Decision]:
        # now is IST. price = bar close; high/low = current bar extremes;
        # volume = this bar's volume (the engine passes it directly to on_tick —
        # there is NO separate on_bar_volume setter). Strategies that don't need
        # volume accept it and ignore it (contract parity).
        ...
    def notify_fill(self, side: str, qty: int, price: float): ...   # update self.position, self.entry_price
    def notify_flat(self): ...
    # Strategies that need the prior session's daily levels expose:
    #   def seed_prior_day(self, prior_high: float, prior_low: float,
    #                      prior_close: float) -> None
    # The engine calls it positionally BEFORE the first on_tick of each session.
```

`Decision` (reuse from `strategies/orb.py`, do not redefine):
`Decision(action="ENTER", side="BUY"|"SELL", stop=float, target=float, reason=str)` or
`Decision(action="EXIT", reason=str)` or `None`.

### Hard rules every strategy MUST follow (copy ORB's structure):
1. **Session reset on date change** — when `now.date()` != last seen, reset all intraday state
   (indicators, session high/low, tried-flags). See `ORB._reset_session`.
2. **EOD square-off is unconditional** — at `15:30 − squareoff_before_close_min`, if
   `self.position != 0` return `Decision(action="EXIT", reason="EOD square-off")`. Must NOT depend
   on any indicator being "ready". (ORB section 3.)
3. **One position at a time**; the engine enforces flat-before-enter. Track `self.position` via
   notify_fill/notify_flat only — never assume your ENTER was executed.
4. **ENTER must carry a concrete `stop` and `target`** (absolute price levels). The engine uses
   these for intrabar (wick) stop/target detection — see refactor note below. If a strategy is
   signal-exit only (e.g. EMA cross-back), still provide a protective `stop`; set `target` to a
   sensible level or a wide far level and emit the real exit via `on_tick` EXIT.
5. **future-skew guard** like ORB (ignore ticks > 2 min ahead of wall clock) — copy it.
6. Indicators are computed incrementally from the (close, high, low) stream within the session
   (no peeking at future bars; no cross-session leakage). Warm-up: return None until enough bars.

## Engine refactor (PREREQUISITE — owned by the refactor coder, not the strategy coders)
`research/backtest/engine.py` currently hardcodes ORB and `_intrabar_exit` recomputes levels from
ORB internals (`orb.or_low/or_high/entry_price/or_range`). Refactor to be strategy-agnostic:
- Capture `decision.stop` and `decision.target` at ENTER time; store on the position state.
- `_intrabar_exit` uses the STORED stop/target (keep the gap-aware + stop-beats-target logic).
- Add a registry `STRATEGIES = {"orb": (ORB, ORBParams), "vwap_mr": (...), ...}` and a
  `--strategy` CLI choice in `research/backtest/__main__.py` (default "orb", preserving current
  behavior). The engine instantiates the chosen class instead of `ORB(...)`.
- Keep the existing gate plumbing (`--gate none|kronos`) intact so ANY strategy can also be
  Kronos-gated for a later A/B.
- ORB behavior and its existing tests MUST remain byte-for-byte unchanged (regression guard).

## Backtest harness (same for all — comparability)
- Period `2024-01-01 → 2026-06-19`, `--split-date 2026-01-01` (IS vs OOS), `--n 10` universe,
  `--slippage-bps 5`, `--equity 500000`. Reads `dhan_clean` (1-min bars).
- Costs: `research/backtest/costs.py` (full Indian intraday stack) — do NOT bypass.
- Report Sharpe (IS + OOS), win%, payoff, profit factor, max DD, trades, net P&L — same as the
  ORB/M3 panel in `research/backtest/report.py`.
- Honest bars: survivorship = CEILING (current scrip master); state it. OOS is the bar.

## Deliverable for a PLANNING agent
Write `research/backtest/strategy_specs/<name>.md` containing: exact entry rule, exact exit rules
(stop, target, signal-exit, EOD), `<Name>Params` fields + defaults, how each indicator is computed
incrementally from the bar stream, warm-up handling, session-reset list, 6-10 concrete unit-test
cases (input bars → expected Decision), and any parameter notes for the backtest. Be concrete
enough that a coder implements it without further research.
