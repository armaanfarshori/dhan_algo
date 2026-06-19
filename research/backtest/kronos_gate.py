"""
Kronos gate adapter for backtests — zero-shot scoring with no lookahead.

Scores the proposed direction using bars up to (and including) the decision
bar, PLUS enough prior-day history to approximate the live gate's context
window (~480 5-min buckets = 2 400 1-min bars), via the same
KronosSignalEngine.score() the live gate uses.  This powers run 2 of the
three-way comparison:

    1. ORB standalone            (gate_fn=None)
    2. ORB + Kronos zero-shot    (this adapter)
    3. ORB + Kronos fine-tuned   (this adapter, KRONOS_CHECKPOINT set)

Inference is CPU-heavy (~seconds per call) but only runs on breakout
decisions — typically a handful per security-day.

No-lookahead guarantee
----------------------
The prior-bar query is ``time < entry_timestamp`` with no upper-bound trick:
we never touch bars from the day being replayed (they come in through
``bars_so_far``).  History is fetched in ascending time order and PREPENDED to
``bars_so_far`` so the combined frame is strictly chronological.
"""
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger("dhan.backtest.kronos")

# 480 5-min buckets × 5 min/bucket = 2 400 raw 1-min rows.  We fetch a few
# extra (HISTORY_FETCH_BUFFER) so that aggregation's trailing-in-progress-bucket
# drop doesn't eat into the target context window.
_TARGET_SCORING_BUCKETS = 480      # match live gate default (config.kronos_lookback)
_BUCKET_MIN = 5                    # 5-min timeframe — NSE pre-training granularity
_HISTORY_1MIN_ROWS = _TARGET_SCORING_BUCKETS * _BUCKET_MIN + 60  # +60 for safety margin


class KronosBacktestGate:
    """
    Backtest adapter for the Kronos scoring engine.

    Parameters
    ----------
    min_confidence : float
        Confidence threshold to ALLOW a trade (mirrors live gate).
    min_history : int
        Minimum combined bars (today + prior) before scoring is attempted.
        Below this the gate fails open (no model — no block).
    seed : int
        Per-run base seed for Kronos Monte-Carlo sampling.  Combined with a
        per-call component (security_id + timestamp) so RNG is deterministic
        for every individual score call WITHOUT perturbing global torch/numpy
        state used by other parts of the backtest run.
    """

    def __init__(self, min_confidence: float = 0.4, min_history: int = 100, seed: int = 0):
        from core.kronos_signal import get_kronos_engine
        self.seed = seed
        self._engine = get_kronos_engine()
        self.min_confidence = min_confidence
        self.min_history = min_history
        self.decisions: list[dict] = []     # audit trail for the report

        # Failure tracking — exposed so callers can detect a degenerate run
        # (high error rate means Run3 silently collapsed to Run1 behaviour).
        self.scoring_attempts: int = 0
        self.scoring_errors: int = 0

    @property
    def error_rate(self) -> Optional[float]:
        """Fraction of score calls that raised an exception, or None if no attempts."""
        if self.scoring_attempts == 0:
            return None
        return self.scoring_errors / self.scoring_attempts

    def log_failure_summary(self) -> None:
        """Emit a summary WARNING when the error rate is non-trivially high."""
        rate = self.error_rate
        if rate is None:
            logger.info("Kronos gate: no scoring attempts recorded.")
            return
        if rate >= 0.10:
            logger.warning(
                "Kronos gate: HIGH ERROR RATE %.1f%% (%d/%d calls failed) — "
                "gated run may be indistinguishable from ungated (Run1). "
                "Inspect logs above for the root cause.",
                rate * 100, self.scoring_errors, self.scoring_attempts,
            )
        else:
            logger.info(
                "Kronos gate: %d/%d scoring calls succeeded (%.1f%% error rate).",
                self.scoring_attempts - self.scoring_errors,
                self.scoring_attempts,
                rate * 100,
            )

    async def __call__(self, security_id: str, direction: str,
                       bars_so_far: pd.DataFrame) -> bool:
        """
        Gate a prospective entry.

        Parameters
        ----------
        security_id : str
            Dhan security ID (string — Dhan WS requires string form).
        direction : str
            "BUY" or "SELL" from the ORB decision.
        bars_so_far : pd.DataFrame
            Today's 1-min bars up to and including the decision bar (no
            lookahead; index or "time" column must be datetime-like).
        """
        # ------------------------------------------------------------------
        # 1.  Prepend prior-day history to widen the scoring context window
        # ------------------------------------------------------------------
        entry_ts = _last_ts(bars_so_far)
        if entry_ts is not None:
            # Fix P2: _fetch_prior_bars now returns (df, failed_flag) so we can
            # record whether the DB fetch errored out (thin context is detectable).
            # Fix P2 (fetch size): pass len(bars_so_far) as extra_rows so we
            # overfetch by the number of same-day bars that _combine will add back
            # — ensuring the prior-day context window is not thinned.
            history_df, prior_bars_failed = _fetch_prior_bars(
                security_id, entry_ts, extra_rows=len(bars_so_far)
            )
        else:
            history_df, prior_bars_failed = pd.DataFrame(), False
        combined = _combine(history_df, bars_so_far)

        if len(combined) < self.min_history:
            self._record(security_id, direction, "ALLOW",
                         f"insufficient history ({len(combined)} bars) — fail-open",
                         prior_bars_failed=prior_bars_failed)
            return True

        df = combined.rename(columns={"time": "ts"})[
            ["ts", "open", "high", "low", "close", "volume"]]

        # ------------------------------------------------------------------
        # 2.  Scoped RNG seeding — deterministic per call, no global bleed
        #
        # We want byte-reproducibility across the three-way runs without
        # torch.manual_seed/np.random.seed perturbing any other computation
        # (e.g. the ORB strategy or portfolio sizing).  Strategy: snapshot the
        # current global RNG state, apply a call-specific seed, run the model,
        # then restore the original state.  The per-call seed is derived from
        # the base seed XOR'd with a hash of (security_id, entry timestamp) so
        # different calls get independent but reproducible seeds.
        # ------------------------------------------------------------------
        call_seed = _call_seed(self.seed, security_id, entry_ts)
        rng_state = _rng_snapshot()
        _rng_seed(call_seed)

        self.scoring_attempts += 1
        try:
            # Pass the per-call seed INTO score() so torch.manual_seed runs inside
            # the executor worker thread (where Kronos sampling happens) — the
            # snapshot/seed above only covers the event-loop thread, whose torch
            # RNG is thread-local and never reaches the worker. This is what makes
            # the three-way runs byte-reproducible.
            result = await self._engine.score(df, seed=call_seed)
        except Exception as exc:
            self.scoring_errors += 1
            logger.warning(
                "Kronos backtest gate failed for %s at %s (%s) — fail-open "
                "[errors so far: %d/%d]",
                security_id, entry_ts, exc,
                self.scoring_errors, self.scoring_attempts,
            )
            self._record(security_id, direction, "ALLOW", f"error: {exc}",
                         prior_bars_failed=prior_bars_failed)
            return True
        finally:
            # Always restore — even on exception — so the caller's RNG is untouched.
            _rng_restore(rng_state)

        side = result.get("side", "HOLD")
        conf = float(result.get("confidence", 0.0))
        agrees = side == direction and conf >= self.min_confidence
        self._record(security_id, direction, "ALLOW" if agrees else "BLOCK",
                     f"model={side} conf={conf:.2f} ctx={len(combined)}",
                     prior_bars_failed=prior_bars_failed)
        return agrees

    def _record(self, sid: str, direction: str, verdict: str, detail: str,
                *, prior_bars_failed: bool = False):
        self.decisions.append({"security_id": sid, "direction": direction,
                               "verdict": verdict, "detail": detail,
                               "prior_bars_failed": prior_bars_failed})


# ---------------------------------------------------------------------------
# Prior-bar history fetch (no-lookahead, reads from the shared engine which is
# already pointed at dhan_clean via init_db(cfg.backtest_db_url) in __main__.py)
# ---------------------------------------------------------------------------

def _last_ts(bars_so_far: pd.DataFrame) -> Optional[pd.Timestamp]:
    """Return the timestamp of the last bar as a UTC-aware Timestamp, or None."""
    if bars_so_far.empty:
        return None
    col = "time" if "time" in bars_so_far.columns else bars_so_far.columns[0]
    try:
        ts = pd.to_datetime(bars_so_far[col].iloc[-1], utc=True)
        return ts
    except Exception:
        return None


def _fetch_prior_bars(
    security_id: str,
    before_ts: pd.Timestamp,
    extra_rows: int = 0,
) -> tuple[pd.DataFrame, bool]:
    """
    Fetch 1-min bars for *security_id* strictly BEFORE *before_ts* from
    dhan_clean.bars.

    Parameters
    ----------
    security_id : str
    before_ts : pd.Timestamp
        Upper-exclusive bound.  MUST be timezone-aware.  If it is tz-naive a
        ``ValueError`` is raised immediately (a tz-naive timestamp interpreted
        as UTC would shift the IST wall-clock by +5:30 and pull in future bars
        — catastrophic look-ahead).
    extra_rows : int
        Additional rows to fetch beyond *_HISTORY_1MIN_ROWS*.  Pass
        ``len(bars_so_far)`` so that the LIMIT accounts for the same-day bars
        that ``_combine`` will append — ensuring the prior-day context window
        is never thinned by today's partial session.

    Returns
    -------
    (df, failed) : tuple[pd.DataFrame, bool]
        *df* is sorted ascending with columns [time, open, high, low, close,
        volume].  *failed* is True when the fetch raised a DB exception (the
        caller should record this in the audit entry so degraded runs are
        detectable post-hoc).  *failed* is False on clean success or when
        there simply are no prior bars.

    The shared db.get_engine() is already pointed at dhan_clean (set up by
    ``init_db(cfg.backtest_db_url)`` in the backtest __main__ before the gate
    is constructed), so no extra connection management is needed here.
    """
    # --- Fix P1: tz-naive guard -----------------------------------------------
    # pd.to_datetime(..., utc=True) on an IST-naive string interprets it as UTC,
    # shifting the bound by −5:30.  We require the caller to pass an aware ts.
    if before_ts.tzinfo is None:
        raise ValueError(
            f"_fetch_prior_bars: before_ts must be timezone-aware, got naive "
            f"timestamp {before_ts!r}.  Wrap with "
            f"pd.Timestamp(..., tz='Asia/Kolkata') or ensure _last_ts() returns "
            f"a UTC-aware value."
        )
    # -------------------------------------------------------------------------

    from sqlalchemy import text
    from db import get_engine

    lim = _HISTORY_1MIN_ROWS + max(0, extra_rows)

    sql = text("""
        SELECT time, open, high, low, close, volume
        FROM bars
        WHERE security_id = :sid
          AND timeframe   = '1m'
          AND time        < :before_ts
        ORDER BY time DESC
        LIMIT :lim
    """)
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(
                sql,
                {"sid": security_id, "before_ts": before_ts, "lim": lim},
            ).fetchall()
    except Exception as exc:
        logger.warning(
            "Kronos gate: prior-bar fetch failed for %s (%s) — "
            "scoring on today-only context (prior_bars_failed=True)",
            security_id, exc,
        )
        return pd.DataFrame(), True   # (empty, failed=True)

    if not rows:
        return pd.DataFrame(), False  # (empty, failed=False — no data, not an error)

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("time").reset_index(drop=True), False


def _combine(history: pd.DataFrame, today: pd.DataFrame) -> pd.DataFrame:
    """
    Merge prior-day history (from DB) with today's bars (from the replay loop).

    ``today`` may have either a "time" column or a DatetimeIndex.  We normalise
    both to a "time" column (UTC-aware) before concatenating.
    """
    if today.empty:
        return history

    today = today.copy()
    if "time" not in today.columns and today.index.name:
        today = today.rename_axis("time").reset_index()
    if "time" in today.columns:
        today["time"] = pd.to_datetime(today["time"], utc=True)

    if history.empty:
        return today

    # Keep only the columns present in both to avoid schema mismatches
    cols = ["time", "open", "high", "low", "close", "volume"]
    history = history[cols].copy()
    today = today[[c for c in cols if c in today.columns]].copy()

    combined = pd.concat([history, today], ignore_index=True)
    combined = combined.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
    return combined


# ---------------------------------------------------------------------------
# RNG scoping helpers
# ---------------------------------------------------------------------------

def _call_seed(base_seed: int, security_id: str, entry_ts: Optional[pd.Timestamp]) -> int:
    """
    Derive a call-specific integer seed that is reproducible for the same
    (base_seed, security_id, entry_ts) triple, but independent across calls.

    We XOR the base seed with the Python hash of the two identifying fields.
    Python's built-in hash is randomised by PYTHONHASHSEED in Python 3.3+, so
    we use a deterministic substitute: hash(str(entry_ts_ns) + security_id)
    computed via the hashlib module, then fold into a 32-bit uint.
    """
    import hashlib
    ts_str = str(int(entry_ts.value)) if entry_ts is not None else "none"
    raw = hashlib.md5(f"{security_id}:{ts_str}".encode()).digest()
    # Take first 4 bytes as little-endian uint32
    call_hash = int.from_bytes(raw[:4], "little")
    return (base_seed ^ call_hash) & 0xFFFF_FFFF


def _rng_snapshot() -> dict:
    """Capture current torch + numpy RNG state (best-effort)."""
    state: dict = {}
    try:
        import torch
        state["torch"] = torch.get_rng_state()
    except Exception:
        pass
    try:
        import numpy as np
        state["numpy"] = np.random.get_state()
    except Exception:
        pass
    return state


def _rng_seed(seed: int) -> None:
    """Apply *seed* to torch and numpy (best-effort; logs a warning on failure)."""
    try:
        import torch
        torch.manual_seed(seed)
    except Exception as exc:
        logger.debug("Kronos gate: could not seed torch RNG (%s)", exc)
    try:
        import numpy as np
        np.random.seed(seed & 0xFFFF_FFFF)
    except Exception as exc:
        logger.debug("Kronos gate: could not seed numpy RNG (%s)", exc)


def _rng_restore(state: dict) -> None:
    """Restore RNG state captured by _rng_snapshot()."""
    if "torch" in state:
        try:
            import torch
            torch.set_rng_state(state["torch"])
        except Exception as exc:
            # Fix P4: warn (not debug) — a failed restore means subsequent scoring
            # calls share RNG state, breaking reproducibility across all three runs.
            logger.warning("Kronos gate: could not restore torch RNG (%s)", exc)
    if "numpy" in state:
        try:
            import numpy as np
            np.random.set_state(state["numpy"])
        except Exception as exc:
            logger.warning("Kronos gate: could not restore numpy RNG (%s)", exc)
