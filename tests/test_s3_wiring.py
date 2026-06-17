"""Tests for the S3 data-path wiring (M2.5 → Kronos → live; backtester clean DB).

These cover the pure-logic links that don't need a GPU/torch or live S3:
  - GAP D: KronosSignalEngine resolves a local checkpoint path as-is.
  - GAP E: config exposes a separate clean-DB URL for the backtester.
  - GAP A: prepare_kronos_dataset timeframe presets + 1m→5m aggregation.
"""
import pandas as pd


# ── GAP E: backtester reads the clean replica ──────────────────────────────────

def test_backtest_db_url_targets_clean_db():
    from config import get_config
    cfg = get_config()
    assert cfg.backtest_db_name == "dhan_clean"
    assert cfg.backtest_db_url.endswith("/dhan_clean")
    # Same host/port/creds as the live URL — only the database name differs.
    assert cfg.backtest_db_url.rsplit("/", 1)[0] == cfg.db_url.rsplit("/", 1)[0]
    assert cfg.db_url.endswith("/dhan_trading")


# ── GAP D: live checkpoint loader resolves local vs s3 ─────────────────────────

def test_resolve_checkpoint_local_passthrough(tmp_path):
    from core.kronos_signal import KronosSignalEngine
    eng = KronosSignalEngine()
    local = str(tmp_path)
    assert eng._resolve_checkpoint(local) == local   # local path returned as-is


def test_engine_reads_checkpoint_config(monkeypatch):
    # The engine must actually consume cfg.kronos_checkpoint (was dead config).
    import config
    config.get_config.cache_clear()
    monkeypatch.setenv("KRONOS_CHECKPOINT", "s3://bucket/kronos/checkpoints/x/")
    from core.kronos_signal import KronosSignalEngine
    eng = KronosSignalEngine()
    assert eng._checkpoint == "s3://bucket/kronos/checkpoints/x/"
    config.get_config.cache_clear()


# ── GAP A: prepare timeframe presets + 5-min aggregation ───────────────────────

def test_tf_presets_match_live_config():
    from config import get_config
    from scripts.prepare_kronos_dataset import TF_PRESETS
    cfg = get_config()
    # 5-min training must match what the model is served with.
    assert TF_PRESETS["5min"]["context"] == cfg.kronos_lookback
    assert TF_PRESETS["5min"]["pred"] == cfg.kronos_pred_len
    assert "1min" in TF_PRESETS


def test_resample_5min_aggregates_ohlcv():
    from scripts.prepare_kronos_dataset import resample_5min
    # 10 one-minute bars in a single 09:15–09:24 window → two 5-min bars.
    idx = pd.date_range("2024-01-01 09:15", periods=10, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "time":   idx,
        "open":   [10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
        "high":   [11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
        "low":    [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
        "close":  [10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5],
        "volume": [100] * 10,
    })
    out = resample_5min(df)
    assert len(out) == 2
    first = out.iloc[0]
    assert first["open"] == 10        # first of bucket
    assert first["close"] == 14.5     # last of bucket
    assert first["high"] == 15        # max of bucket
    assert first["low"] == 9          # min of bucket
    assert first["volume"] == 500     # sum of bucket
