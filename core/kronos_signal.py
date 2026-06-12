"""
Kronos Signal Engine
====================
Wraps KronosPredictor (vendored in kronos/) to produce BUY/SELL/HOLD
signal scores from historical 1-min OHLCV data.

Usage:
    engine = KronosSignalEngine()
    await engine.load()                            # downloads model once

    # Using OHLCV from TimescaleDB
    signal = await engine.score_from_db(security_id="2885", lookback=400)

    # Or from a raw DataFrame already in memory
    signal = await engine.score(ohlcv_df, pred_len=30)

Returns a dict:
    {
        "side":       "BUY" | "SELL" | "HOLD",
        "score":      float,         # abs forecasted return
        "confidence": float,         # 0–1 from sampling spread
        "forecast_df": pd.DataFrame  # full N-candle forecast
    }
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("dhan.kronos_signal")

# Defaults — overridden by Config at class init time
_TOKENIZER_ID  = "NeoQuasar/Kronos-Tokenizer-base"
_MODEL_ID      = "NeoQuasar/Kronos-small"
_LOOKBACK      = 480
_PRED_LEN      = 6
_SAMPLE_COUNT  = 10
_SIGNAL_THRESH = 0.001


def scorer_version() -> str:
    """
    Identifies the scoring configuration on every persisted gate verdict.
    Calibration must never pool outcomes across configs — a threshold tuned
    on one scorer says nothing about another.
    """
    from config import get_config
    c = get_config()
    return (f"v2-{c.kronos_timeframe}-T{c.kronos_temperature}"
            f"-N{c.kronos_samples}-L{c.kronos_lookback}-P{c.kronos_pred_len}")


def _aggregate_bars(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Aggregate 1-min OHLCV (datetime index) into `timeframe` buckets.
    Uses groupby-on-floor rather than resample so overnight/holiday gaps
    don't produce empty buckets. Drops the trailing in-progress bucket —
    Kronos was pre-trained on complete K-lines only.
    """
    if timeframe in ("1min", "1m", "T"):
        return df
    floor = df.index.floor(timeframe)
    agg = df.groupby(floor).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"))
    if len(agg) > 1:
        bucket = pd.Timedelta(timeframe)
        # complete iff the raw data reaches the bucket's final minute
        if df.index[-1] < agg.index[-1] + bucket - pd.Timedelta(minutes=1):
            agg = agg.iloc[:-1]
    return agg


class KronosSignalEngine:
    """
    Lazy-loads Kronos from HuggingFace on first call to .load().
    Thread-safe for a single asyncio event loop.
    """

    def __init__(self):
        self._predictor = None
        self._lock = asyncio.Lock()
        from config import get_config
        cfg = get_config()
        self._tokenizer_id  = cfg.kronos_tokenizer
        self._model_id      = cfg.kronos_model
        self._timeframe     = cfg.kronos_timeframe
        self._lookback      = cfg.kronos_lookback
        self._pred_len      = cfg.kronos_pred_len
        self._sample_count  = cfg.kronos_samples
        self._temperature   = cfg.kronos_temperature
        self._top_p         = cfg.kronos_top_p
        self._signal_thresh = cfg.kronos_thresh
        self._bucket_min    = max(1, int(pd.Timedelta(self._timeframe).total_seconds() // 60))

    async def load(self, device: str = "cpu"):
        """Download and cache the model. Safe to call multiple times."""
        async with self._lock:
            if self._predictor is not None:
                return
            loop = asyncio.get_event_loop()
            logger.info("Loading Kronos from HuggingFace (%s / %s)…", self._tokenizer_id, self._model_id)
            self._predictor = await loop.run_in_executor(None, self._load_sync, device)
            logger.info("Kronos loaded.")

    def _load_sync(self, device: str):
        from kronos import KronosTokenizer, Kronos, KronosPredictor
        tokenizer = KronosTokenizer.from_pretrained(self._tokenizer_id)
        model     = Kronos.from_pretrained(self._model_id)
        return KronosPredictor(model, tokenizer, max_context=512)

    # ------------------------------------------------------------------
    # Core scoring
    # ------------------------------------------------------------------

    async def score(
        self,
        ohlcv_df: pd.DataFrame,
        pred_len: Optional[int] = None,
        sample_count: Optional[int] = None,
    ) -> dict:
        """
        Generate a directional signal from a 1-min OHLCV DataFrame.

        Input is aggregated to the configured scoring timeframe (default
        5-min — Kronos's NSE pre-training granularity; it never saw 1-min
        NSE bars). ohlcv_df must have columns open/high/low/close/volume
        and a datetime index (or a 'ts' column).
        """
        pred_len = pred_len or self._pred_len
        sample_count = sample_count or self._sample_count
        if self._predictor is None:
            await self.load()

        df = _prepare_df(ohlcv_df)
        df = _aggregate_bars(df, self._timeframe)
        if len(df) < 10:
            return _hold("Not enough history")

        lookback = min(self._lookback, len(df))
        x_df = df.iloc[-lookback:][["open", "high", "low", "close", "volume"]].copy()
        x_df["amount"] = x_df["volume"] * x_df["close"]  # synthetic amount

        last_ts = x_df.index[-1]
        step = pd.Timedelta(self._timeframe)
        y_ts = pd.date_range(start=last_ts + step, periods=pred_len, freq=self._timeframe)

        loop = asyncio.get_event_loop()
        pred_df = await loop.run_in_executor(
            None,
            self._predict_sync,
            x_df.reset_index(),
            pd.Series(x_df.index),
            pd.Series(y_ts),
            pred_len,
            sample_count,
        )

        result = _compute_signal(x_df, pred_df, pred_len)
        result["scorer_version"] = scorer_version()
        return result

    def _predict_sync(self, x_df, x_ts, y_ts, pred_len, sample_count):
        # T/top_p follow the paper's price/return-forecasting protocol
        # (T=0.6, top_p=0.90) — see docs/Kronos-Model-Notes.md.
        return self._predictor.predict(
            df=x_df,
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=pred_len,
            T=self._temperature,
            top_p=self._top_p,
            sample_count=sample_count,
            verbose=False,
        )

    # ------------------------------------------------------------------
    # TimescaleDB-backed scoring
    # ------------------------------------------------------------------

    async def score_from_db(
        self,
        security_id: str,
        exchange_segment: str = "NSE_EQ",
        lookback: Optional[int] = None,
        pred_len: Optional[int] = None,
    ) -> dict:
        """Fetch 1-min OHLCV from TimescaleDB and score with Kronos."""
        # lookback is in scoring-timeframe bars; fetch enough raw 1-min rows
        # to fill those buckets (+1 for the dropped in-progress bucket).
        raw_rows = ((lookback or self._lookback) + 1) * self._bucket_min
        try:
            df = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_ohlcv_sync, security_id, exchange_segment, raw_rows
            )
        except Exception as exc:
            logger.warning("DB fetch failed for %s: %s", security_id, exc)
            return _hold(f"DB error: {exc}")

        if df.empty:
            return _hold("No data in DB")

        result = await self.score(df, pred_len=pred_len)
        # Data freshness — the gate logs this so calibration can separate
        # decisions made on live bars from ones made on stale history.
        try:
            last_ts = pd.to_datetime(df["ts"].iloc[-1], utc=True)
            result["last_bar_ts"] = last_ts.isoformat()
            result["data_age_min"] = round(
                (datetime.now(timezone.utc) - last_ts).total_seconds() / 60, 1)
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------
    # Batch scoring (multiple securities)
    # ------------------------------------------------------------------

    async def score_batch(
        self,
        security_ids: list[str],
        exchange_segment: str = "NSE_EQ",
        pred_len: int = _PRED_LEN,
    ) -> dict[str, dict]:
        """Score multiple securities concurrently."""
        tasks = {
            sid: self.score_from_db(sid, exchange_segment, pred_len=pred_len)
            for sid in security_ids
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            sid: (r if not isinstance(r, Exception) else _hold(str(r)))
            for sid, r in zip(tasks.keys(), results)
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "ts" in df.columns:
        df = df.set_index("ts")
    df.index = pd.to_datetime(df.index, utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df.sort_index()


def _fetch_ohlcv_sync(security_id: str, exchange_segment: str, lookback: int) -> pd.DataFrame:
    # Reads the compressed bars hypertable. The uncompressed ohlcv_1min
    # mirror was dropped 2026-06-11 — it duplicated bars at 6× the size and
    # its double-writes were OOM-killing Postgres. exchange_segment is
    # unused: Dhan security_ids are unique within the equity universe.
    from sqlalchemy import text
    from db import get_engine
    sql = text("""
        SELECT time AS ts, open, high, low, close, volume
        FROM bars
        WHERE security_id = :sid AND timeframe = '1m'
        ORDER BY time DESC
        LIMIT :lim
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql, {"sid": security_id, "lim": lookback})
        rows = result.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.sort_values("ts").reset_index(drop=True)


def _compute_signal(x_df: pd.DataFrame, pred_df: pd.DataFrame, pred_len: int) -> dict:
    """Turn a forecast DataFrame into a BUY/SELL/HOLD signal with score/confidence."""
    current_price = float(x_df["close"].iloc[-1])
    if pred_df is None or pred_df.empty:
        return _hold("Empty forecast")

    pred_close = pred_df["close"].values
    # Horizon = end of forecast window
    horizon_price = float(np.median(pred_close))
    forecasted_return = (horizon_price - current_price) / (current_price + 1e-9)

    # Confidence from spread of close predictions across samples
    confidence = 1.0 - min(float(np.std(pred_close)) / (current_price + 1e-9) * 10, 1.0)
    score = abs(forecasted_return)

    if forecasted_return > _SIGNAL_THRESH:
        side = "BUY"
    elif forecasted_return < -_SIGNAL_THRESH:
        side = "SELL"
    else:
        side = "HOLD"

    logger.info(
        "Kronos signal: %s  return=%.4f  confidence=%.2f  horizon_px=%.2f  current=%.2f",
        side, forecasted_return, confidence, horizon_price, current_price,
    )
    return {
        "side": side,
        "score": round(score, 6),
        "confidence": round(confidence, 4),
        "forecasted_return": round(forecasted_return, 6),
        "current_price": current_price,
        "horizon_price": horizon_price,
        "forecast_df": pred_df,
    }


def _hold(reason: str) -> dict:
    return {
        "side": "HOLD",
        "score": 0.0,
        "confidence": 0.0,
        "forecasted_return": 0.0,
        "reason": reason,
        "forecast_df": pd.DataFrame(),
    }


# Singleton for use across the platform
_engine: Optional[KronosSignalEngine] = None


def get_kronos_engine() -> KronosSignalEngine:
    global _engine
    if _engine is None:
        _engine = KronosSignalEngine()
    return _engine
