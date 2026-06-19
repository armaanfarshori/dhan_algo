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
_LOOKBACK      = 480
_PRED_LEN      = 6
_SAMPLE_COUNT  = 10
_SIGNAL_THRESH = 0.001


def scorer_version() -> str:
    """
    Identifies the scoring configuration on every persisted gate verdict.
    Calibration must never pool outcomes across configs — a threshold tuned
    on one scorer says nothing about another.

    When a fine-tuned checkpoint is active the last path segment is appended
    so that zero-shot and fine-tuned verdicts are never pooled under the same
    calibration key.  When KRONOS_CHECKPOINT is empty the string is
    byte-identical to the pre-fix value.
    """
    from config import get_config
    c = get_config()
    base = (f"v2-{c.kronos_timeframe}-T{c.kronos_temperature}"
            f"-N{c.kronos_samples}-L{c.kronos_lookback}-P{c.kronos_pred_len}")
    ckpt = (c.kronos_checkpoint or "").strip()
    if ckpt:
        # Use the last non-empty path segment as a short, stable token.
        token = [s for s in ckpt.rstrip("/").split("/") if s][-1]
        return f"{base}-ckpt{token}"
    return base


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
        self._offline       = cfg.kronos_offline
        self._revision      = cfg.kronos_revision or None
        self._checkpoint    = (cfg.kronos_checkpoint or "").strip()
        self._loaded_from   = ""
        self._bucket_min    = max(1, int(pd.Timedelta(self._timeframe).total_seconds() // 60))

    async def load(self, device: str = "cpu"):
        """Load the model (from local cache by default). Safe to call repeatedly."""
        async with self._lock:
            if self._predictor is not None:
                return
            loop = asyncio.get_running_loop()
            logger.info("Loading Kronos (%s / %s, offline=%s)…",
                        self._tokenizer_id, self._model_id, self._offline)
            self._predictor = await loop.run_in_executor(None, self._load_sync, device)
            logger.info("Kronos loaded from %s.", self._loaded_from)

    def _resolve_checkpoint(self, ckpt: str) -> str:
        """Resolve a checkpoint reference to a local directory.

        Local path → returned as-is. `s3://bucket/prefix/` → synced ONCE into a
        local cache dir (subsequent starts reuse it), then that dir is returned.
        The dir is expected to contain BOTH the model and tokenizer files
        (finetune.py saves them together into output_dir/best)."""
        if not ckpt.startswith("s3://"):
            return ckpt

        without = ckpt[len("s3://"):]
        bucket, _, prefix = without.partition("/")
        prefix = prefix.rstrip("/")
        cache_root = os.path.expanduser("~/.cache/dhan_kronos")
        local_dir = os.path.join(cache_root, bucket, prefix)

        # Reuse an already-synced checkpoint (presence of any file == synced).
        if os.path.isdir(local_dir) and any(os.scandir(local_dir)):
            logger.info("Kronos checkpoint cache hit: %s", local_dir)
            return local_dir

        import boto3
        os.makedirs(local_dir, exist_ok=True)
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        n = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel = key[len(prefix) + 1:]
                dest = os.path.join(local_dir, rel)
                os.makedirs(os.path.dirname(dest) or local_dir, exist_ok=True)
                s3.download_file(bucket, key, dest)
                n += 1
        if n == 0:
            raise FileNotFoundError(f"No objects under {ckpt}")
        logger.info("Synced %d checkpoint files from %s → %s", n, ckpt, local_dir)
        return local_dir

    def _load_sync(self, device: str):
        from kronos import KronosTokenizer, Kronos, KronosPredictor

        # Fine-tuned checkpoint (local dir or s3:// URI). Fail-OPEN: if it can't
        # be resolved/loaded, fall through to the HF zero-shot path below rather
        # than leave the gate dead (Kronos errors must never block trades).
        if self._checkpoint:
            try:
                local_dir = self._resolve_checkpoint(self._checkpoint)
                # The checkpoint is MODEL-ONLY: fine-tuning freezes the tokenizer,
                # so it never changes. Co-saving both to one dir clobbers them
                # (both write config.json + model.safetensors), so the tokenizer
                # loads from its own repo and only the model from the checkpoint.
                tok = KronosTokenizer.from_pretrained(self._tokenizer_id)
                mdl = Kronos.from_pretrained(local_dir)
                self._loaded_from = f"fine-tuned checkpoint {self._checkpoint}"
                return KronosPredictor(mdl, tok, max_context=512)
            except Exception as exc:
                logger.critical(
                    "Kronos checkpoint load FAILED (%s) — falling back to zero-shot %s",
                    exc, self._model_id)

        def _fetch(offline: bool):
            tok = KronosTokenizer.from_pretrained(
                self._tokenizer_id, revision=self._revision, local_files_only=offline)
            mdl = Kronos.from_pretrained(
                self._model_id, revision=self._revision, local_files_only=offline)
            return tok, mdl

        if self._offline:
            try:
                tokenizer, model = _fetch(offline=True)
                self._loaded_from = "local cache"
            except Exception as exc:
                # Fresh box / cache cleared — pull once, then offline next start.
                logger.warning("Kronos not in local cache (%s) — downloading once "
                               "from HuggingFace", exc)
                tokenizer, model = _fetch(offline=False)
                self._loaded_from = "HuggingFace (first download — cached for next start)"
        else:
            tokenizer, model = _fetch(offline=False)
            self._loaded_from = "HuggingFace"
        return KronosPredictor(model, tokenizer, max_context=512)

    # ------------------------------------------------------------------
    # Core scoring
    # ------------------------------------------------------------------

    async def score(
        self,
        ohlcv_df: pd.DataFrame,
        pred_len: Optional[int] = None,
        sample_count: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> dict:
        """
        Generate a directional signal from a 1-min OHLCV DataFrame.

        Input is aggregated to the configured scoring timeframe (default
        5-min — Kronos's NSE pre-training granularity; it never saw 1-min
        NSE bars). ohlcv_df must have columns open/high/low/close/volume
        and a datetime index (or a 'ts' column).

        seed: when provided, torch.manual_seed(seed) is called inside the
        executor thread immediately before predictor.predict(), making
        torch.multinomial deterministic for that call.  Default None leaves
        live behavior unchanged (no seeding).
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

        loop = asyncio.get_running_loop()
        pred_df = await loop.run_in_executor(
            None,
            self._predict_sync,
            x_df.reset_index(),
            pd.Series(x_df.index),
            pd.Series(y_ts),
            pred_len,
            sample_count,
            seed,
        )

        result = _compute_signal(x_df, pred_df, pred_len,
                                 signal_thresh=self._signal_thresh)
        result["scorer_version"] = scorer_version()
        return result

    def _predict_sync(self, x_df, x_ts, y_ts, pred_len, sample_count, seed):
        # seed is threaded from score() for deterministic backtest replay.
        # None (live default) = no seeding; torch RNG is left untouched.
        if seed is not None:
            import torch
            torch.manual_seed(seed)
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
            df = await asyncio.get_running_loop().run_in_executor(
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
        except Exception as exc:
            logger.debug("kronos: failed to compute data_age_min (%s)", exc)
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


def _directional_confidence(pred_close: np.ndarray, current_price: float) -> float:
    """
    Compute a [0, 1] confidence score from the spread of sampled close-price
    forecasts relative to the current price.

    Background
    ----------
    KronosPredictor runs Monte-Carlo sampling (sample_count N draws) over the
    forecast horizon.  Each draw produces a sequence of predicted closes; we
    receive ``pred_close`` — a 1-D array of length (N * pred_len) or (pred_len,)
    depending on whether the predictor flattens samples.  When the model is
    uncertain, the draws fan out widely; when it is confident, the draws cluster
    tightly around a consensus trajectory.

    Formula
    -------
    We measure dispersion as the *coefficient of variation* (std / price) — a
    price-normalised standard deviation — then map it linearly into [0, 1] with
    a fixed scale factor of 10, clamping to [0, 1]:

        rel_std    = std(pred_close) / (current_price + 1e-9)
        confidence = 1.0 - clamp(rel_std * 10, 0, 1)

    The scale factor of 10 means:
      • rel_std ≤ 0.0   → confidence = 1.0  (no spread at all — maximally certain)
      • rel_std = 0.05  → confidence = 0.5  (5 % price spread across samples)
      • rel_std = 0.10  → confidence = 0.0  (10 % price spread — maximally uncertain)
      • rel_std > 0.10  → confidence = 0.0  (clamped)

    For an NSE equity priced around ₹200–₹2000, a 5 % inter-sample spread in
    the predicted horizon price (₹10–₹100 range) is a reasonable mid-point,
    giving the gate its observed live values in the 0.85–0.99 region.

    Inputs
    ------
    pred_close    : np.ndarray — all close-price predictions across samples and
                    timesteps (flattened or 1-D horizon slice from pred_df).
    current_price : float — last observed close price at scoring time, used only
                    for price-normalisation (cannot be 0; guarded by + 1e-9).

    Output
    ------
    float in [0, 1].  High value = samples agree tightly (act on signal).
                      Low value  = samples diverge widely (noisy / ambiguous).

    Calibration note
    ----------------
    This formula is baked into ``scorer_version()`` implicitly (the version
    string does not yet encode the scale factor 10).  Any change to the numeric
    output — including changing the 10× scale or the normalisation base — MUST
    be accompanied by a ``scorer_version`` bump so in-flight calibration data
    is not pooled with data produced under a different formula.

    # REVIEW: The scale factor 10 is an arbitrary constant chosen so that
    # typical NSE price spreads land in a useful part of [0, 1].  It was never
    # empirically calibrated against realised accuracy.  A principled
    # alternative would be to divide by the *expected move* implied by recent
    # ATR (e.g. rel_std / atr_pct) so the constant becomes stock-agnostic.
    # Implementing that requires passing ATR into this function and bumping
    # scorer_version — do not do so until the current calibration run (≥30
    # fresh verdicts) completes and provides a baseline to compare against.
    """
    rel_std = float(np.std(pred_close)) / (current_price + 1e-9)
    return 1.0 - min(rel_std * 10, 1.0)


def _compute_signal(
    x_df: pd.DataFrame,
    pred_df: pd.DataFrame,
    pred_len: int,
    signal_thresh: float = _SIGNAL_THRESH,
) -> dict:
    """Turn a forecast DataFrame into a BUY/SELL/HOLD signal with score/confidence.

    signal_thresh is passed in from the caller (KronosSignalEngine.score passes
    self._signal_thresh, which is read from cfg.kronos_thresh).  The default
    equals _SIGNAL_THRESH so direct callers that don't supply the argument get
    identical output.
    """
    current_price = float(x_df["close"].iloc[-1])
    if pred_df is None or pred_df.empty:
        return _hold("Empty forecast")

    pred_close = pred_df["close"].values
    # Horizon = end of forecast window
    horizon_price = float(np.median(pred_close))
    forecasted_return = (horizon_price - current_price) / (current_price + 1e-9)

    confidence = _directional_confidence(pred_close, current_price)
    score = abs(forecasted_return)

    if forecasted_return > signal_thresh:
        side = "BUY"
    elif forecasted_return < -signal_thresh:
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
