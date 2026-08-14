"""AsyncDBBackend enablement — explicit JOURNAL_DB_ENABLED, no localhost heuristic.

The AWS-era predicate treated DB_HOST=localhost as "dev box, no DB" and
silently disabled journalling. On a single-host deploy localhost IS the
production DB; with journalling dead the trades table stays empty and
RiskEngine's realized-loss meters (refresh_pnl) read zero forever. These tests
pin the replacement contract:

  • default config (localhost DB) → journalling ENABLED
  • JOURNAL_DB_ENABLED=false      → disabled, regardless of host
  • DB_HOST=""                    → disabled (nothing to connect to)
"""
from __future__ import annotations

from config import Config
from core.journal import AsyncDBBackend


def _backend_with(monkeypatch, **cfg_kwargs) -> AsyncDBBackend:
    cfg = Config(_env_file=None, **cfg_kwargs)
    monkeypatch.setattr("config.get_config", lambda: cfg)
    return AsyncDBBackend()


def test_default_localhost_is_enabled(monkeypatch):
    be = _backend_with(monkeypatch)
    assert be._enabled is True


def test_explicit_disable_wins(monkeypatch):
    be = _backend_with(monkeypatch, journal_db_enabled=False)
    assert be._enabled is False


def test_empty_db_host_disables(monkeypatch):
    be = _backend_with(monkeypatch, db_host="")
    assert be._enabled is False


def test_remote_host_still_enabled(monkeypatch):
    be = _backend_with(monkeypatch, db_host="10.0.0.5")
    assert be._enabled is True
