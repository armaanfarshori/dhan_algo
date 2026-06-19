"""
Strategy registry — maps strategy names to (StrategyClass, ParamsClass) pairs.

To add a new strategy:
1. Create ``strategies/<name>.py`` implementing the contract in
   ``research/backtest/strategy_specs/_CONTRACT.md``.
2. Import the class and its params dataclass below.
3. Add one line: ``STRATEGIES["<name>"] = (<Name>, <Name>Params)``

The backtester CLI (``python -m research.backtest --strategy <name>``) will then
accept the new name, instantiate ``<Name>(security_id, <Name>Params())`` per
security, and run the same engine / cost stack as ORB.

Required strategy interface (Protocol):

    class StrategyProtocol:
        def on_tick(
            self, now: datetime, price: float,
            high: Optional[float] = None,
            low: Optional[float] = None,
        ) -> Optional[Decision]: ...
        def notify_fill(self, side: str, qty: int, price: float) -> None: ...
        def notify_flat(self) -> None: ...

``on_tick`` must return:
  - ``Decision(action="ENTER", side="BUY"|"SELL", stop=<float>, target=<float>, reason=<str>)``
    when a new position should be opened.  ``stop`` and ``target`` are ABSOLUTE price
    levels; the engine stores them and uses them for intrabar wick detection.
  - ``Decision(action="EXIT", reason=<str>)`` to close the current position.
  - ``None`` to do nothing.

See ``research/backtest/strategy_specs/_CONTRACT.md`` for the full contract
(session-reset, EOD square-off, future-skew guard, etc.).
"""

from strategies.orb import ORB, ORBParams

# Maps CLI name → (StrategyClass, ParamsClass).
# Adding a strategy: append one entry; no other file needs to change.
STRATEGIES: dict[str, tuple[type, type]] = {
    "orb": (ORB, ORBParams),
}
