"""
Kronos gate adapter for backtests — zero-shot scoring with no lookahead.

Scores the proposed direction using ONLY the bars passed in (everything up to
and including the decision bar), via the same KronosSignalEngine.score() the
live gate uses. This powers run 2 of the three-way comparison:

    1. ORB standalone            (gate_fn=None)
    2. ORB + Kronos zero-shot    (this adapter)
    3. ORB + Kronos fine-tuned   (this adapter, KRONOS_CHECKPOINT set)

Inference is CPU-heavy (~seconds per call) but only runs on breakout
decisions — typically a handful per security-day.
"""
import logging

import pandas as pd

logger = logging.getLogger("dhan.backtest.kronos")


class KronosBacktestGate:
    def __init__(self, min_confidence: float = 0.4, min_history: int = 100, seed: int = 0):
        from core.kronos_signal import get_kronos_engine
        # REPRODUCIBILITY: Kronos sampling (torch.multinomial, T=0.6, N=10) is
        # stochastic. Seed torch + numpy so a backtest run is byte-reproducible —
        # the M3 three-way comparison must be deterministic given fixed inputs.
        try:
            import torch
            import numpy as np
            torch.manual_seed(seed)
            np.random.seed(seed)
        except Exception as exc:
            logger.warning("Kronos gate: could not seed RNG (%s) — runs may not be reproducible", exc)
        self.seed = seed
        self._engine = get_kronos_engine()
        self.min_confidence = min_confidence
        self.min_history = min_history
        self.decisions: list[dict] = []     # audit trail for the report

    async def __call__(self, security_id: str, direction: str,
                       bars_so_far: pd.DataFrame) -> bool:
        if len(bars_so_far) < self.min_history:
            self._record(security_id, direction, "ALLOW", "insufficient history — fail-open")
            return True
        df = bars_so_far.rename(columns={"time": "ts"})[
            ["ts", "open", "high", "low", "close", "volume"]]
        try:
            result = await self._engine.score(df)
        except Exception as exc:
            logger.warning("Kronos backtest gate failed for %s (%s) — fail-open",
                           security_id, exc)
            self._record(security_id, direction, "ALLOW", f"error: {exc}")
            return True

        side = result.get("side", "HOLD")
        conf = float(result.get("confidence", 0.0))
        agrees = side == direction and conf >= self.min_confidence
        self._record(security_id, direction, "ALLOW" if agrees else "BLOCK",
                     f"model={side} conf={conf:.2f}")
        return agrees

    def _record(self, sid: str, direction: str, verdict: str, detail: str):
        self.decisions.append({"security_id": sid, "direction": direction,
                               "verdict": verdict, "detail": detail})
