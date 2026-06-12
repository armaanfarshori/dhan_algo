"""
Kronos gate — scores a proposed trade direction and PERSISTS every decision.

Shadow mode (default): the verdict is computed, logged, and written to the
signals table, but never enforced. This builds the calibration record that
Phase 3 turns into an evidence-based threshold. Decisions made before
2026-06-11 were scored on stale bars and are excluded from calibration by
construction (the persisted rows carry data_age_min).

Enforcing mode: a trade is allowed only when the model agrees with the
direction AND confidence ≥ min_confidence. Fail-open on any model error —
the gate must never be the reason an exit or entry hangs.
"""
import logging
from typing import Optional

logger = logging.getLogger("dhan.ml.kronos_gate")

# Data older than this during market hours is flagged stale — the forecast
# is anchored in the past and shouldn't count toward calibration.
STALE_AFTER_MIN = 15


class KronosGate:
    def __init__(self, engine, db_backend=None, min_confidence: float = 0.4,
                 shadow: bool = True):
        self._engine = engine          # KronosSignalEngine (lazy-loads model)
        self._db = db_backend
        self.min_confidence = min_confidence
        self.shadow = shadow

    async def allows(self, security_id: str, direction: str,
                     exchange_segment: str = "NSE_EQ",
                     strategy: str = "ORB") -> bool:
        """Score the direction; persist the decision; enforce only if not shadow."""
        try:
            result = await self._engine.score_from_db(
                security_id=security_id, exchange_segment=exchange_segment)
        except Exception as exc:
            logger.warning("Kronos gate failed for %s (%s) — allowing (fail-open)",
                           security_id, exc)
            return True

        side = result.get("side", "HOLD")
        confidence = float(result.get("confidence", 0.0))
        data_age = result.get("data_age_min")
        stale = data_age is None or data_age > STALE_AFTER_MIN
        agrees = (side == direction) and (confidence >= self.min_confidence)
        verdict = "ALLOW" if agrees else "BLOCK"

        logger.info("[%s] %sKronos says %s (conf=%.2f, age=%smin%s)  %s wants %s → %s%s",
                    security_id, "[SHADOW] " if self.shadow else "",
                    side, confidence, data_age, " STALE" if stale else "",
                    strategy, direction, verdict,
                    " (not enforced)" if self.shadow else "")

        await self._persist(security_id, direction, verdict, result, stale, strategy)
        return True if self.shadow else agrees

    async def _persist(self, security_id: str, direction: str, verdict: str,
                       result: dict, stale: bool, strategy: str):
        if not self._db:
            return
        try:
            await self._db.log_signal(
                security_id=security_id,
                side=result.get("side", "HOLD"),
                strategy="orb_gate",
                score=float(result.get("score", 0.0)),
                confidence=float(result.get("confidence", 0.0)),
                features={
                    "requested_direction": direction,
                    "verdict": verdict,
                    "enforced": not self.shadow,
                    "shadow": self.shadow,
                    "forecasted_return": result.get("forecasted_return", 0.0),
                    "current_price": result.get("current_price", 0.0),
                    "horizon_price": result.get("horizon_price", 0.0),
                    "last_bar_ts": result.get("last_bar_ts"),
                    "data_age_min": result.get("data_age_min"),
                    "stale": stale,
                    "min_confidence": self.min_confidence,
                    "caller_strategy": strategy,
                    # Calibration groups by this — outcomes from different
                    # scoring configs (timeframe/T/N) must never be pooled.
                    "scorer_version": result.get("scorer_version"),
                },
            )
        except Exception as exc:
            logger.warning("Kronos gate: failed to persist decision (%s)", exc)
