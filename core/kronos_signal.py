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
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("dhan.kronos_signal")

# Defaults — overridden by Config at class init time
_TOKENIZER_ID  = "NeoQuasar/Kronos-Tokenizer-base"
_MODEL_ID      = "NeoQuasar/Kronos-small"
_LOOKBACK      = 400
_PRED_LEN      = 30
_SAMPLE_COUNT  = 5
_SIGNAL_THRESH = 0.001


class KronosSignalEngine:
    """
    Lazy-loads Kronos from HuggingFace on first call to .load().
    Thread-safe for a single asyncio event loop.
    """

    def __init__(self):
        self._predictor = None
        self._lock = asyncio.Lock()
        # Read config lazily at instantiation so .env is already loaded
        try:
            from config import get_config
            cfg = get_config()
            self._tokenizer_id  = os.getenv("KRONOS_TOKENIZER", _TOKENIZER_ID)
            self._model_id      = os.getenv("KRONOS_MODEL",     _MODEL_ID)
            self._lookback      = int(os.getenv("KRONOS_LOOKBACK",  str(_LOOKBACK)))
            self._pred_len      = int(os.getenv("KRONOS_PRED_LEN",  str(_PRED_LEN)))
            self._sample_count  = int(os.getenv("KRONOS_SAMPLES",   str(_SAMPLE_COUNT)))
            self._signal_thresh = float(os.getenv("KRONOS_THRESH",  str(_SIGNAL_THRESH)))
        except Exception:
            self._tokenizer_id  = _TOKENIZER_ID
            self._model_id      = _MODEL_ID
            self._lookback      = _LOOKBACK
            self._pred_len      = _PRED_LEN
            self._sample_count  = _SAMPLE_COUNT
            self._signal_thresh = _SIGNAL_THRESH

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
        pred_len: int = _PRED_LEN,
        sample_count: int = _SAMPLE_COUNT,
    ) -> dict:
        """
        Generate a directional signal from an OHLCV DataFrame.

        ohlcv_df must have columns: open, high, low, close, volume
        Index should be datetime (or a 'ts' column).
        """
        if self._predictor is None:
            await self.load()

        df = _prepare_df(ohlcv_df)
        if len(df) < 10:
            return _hold("Not enough history")

        lookback = min(_LOOKBACK, len(df))
        x_df = df.iloc[-lookback:][["open", "high", "low", "close", "volume"]].copy()
        x_df["amount"] = x_df["volume"] * x_df["close"]  # synthetic amount

        last_ts = x_df.index[-1]
        # Build future timestamps (1-min bars)
        y_ts = pd.date_range(start=last_ts + timedelta(minutes=1), periods=pred_len, freq="1min")

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

        return _compute_signal(x_df, pred_df, pred_len)

    def _predict_sync(self, x_df, x_ts, y_ts, pred_len, sample_count):
        return self._predictor.predict(
            df=x_df,
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=pred_len,
            T=1.0,
            top_p=0.9,
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
        lookback: int = _LOOKBACK,
        pred_len: int = _PRED_LEN,
    ) -> dict:
        """Fetch OHLCV from TimescaleDB and score with Kronos."""
        try:
            df = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_ohlcv_sync, security_id, exchange_segment, lookback
            )
        except Exception as exc:
            logger.warning("DB fetch failed for %s: %s", security_id, exc)
            return _hold(f"DB error: {exc}")

        if df.empty:
            return _hold("No data in DB")

        return await self.score(df, pred_len=pred_len)

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
    from sqlalchemy import text
    from db import get_engine
    sql = text("""
        SELECT ts, open, high, low, close, volume
        FROM ohlcv_1min
        WHERE security_id = :sid AND exchange_segment = :seg
        ORDER BY ts DESC
        LIMIT :lim
    """)
    with get_engine().connect() as conn:
        result = conn.execute(sql, {"sid": security_id, "seg": exchange_segment, "lim": lookback})
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
