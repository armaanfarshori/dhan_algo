"""Config sanity — typed settings load with safe defaults and parse env vars."""
import os

from config import Config


def test_defaults_are_safe():
    cfg = Config(_env_file=None)
    assert cfg.paper_trading is True, "must default to paper"
    assert cfg.allow_live_toggle is False, "live toggle must default off"
    assert cfg.kronos_shadow_mode is True, "gate must default to shadow until calibrated"
    assert cfg.webhook_port == 8765
    assert cfg.watchlist_n == 20


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("WATCHLIST_N", "8")
    monkeypatch.setenv("KRONOS_MIN_CONFIDENCE", "0.55")
    cfg = Config(_env_file=None)
    assert cfg.paper_trading is False
    assert cfg.watchlist_n == 8
    assert cfg.kronos_min_confidence == 0.55


def test_db_url_quotes_password(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "p@ss:word/1")
    cfg = Config(_env_file=None)
    assert "p%40ss%3Aword%2F1" in cfg.db_url


def test_bad_numeric_fails_loudly(monkeypatch):
    monkeypatch.setenv("WATCHLIST_N", "five")
    import pytest
    with pytest.raises(Exception):
        Config(_env_file=None)
