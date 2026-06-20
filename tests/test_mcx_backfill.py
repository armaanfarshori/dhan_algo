"""Tests for core/mcx_backfill.py — pure parse + off-hours-guarded orchestration
+ CLI arg-parsing + token path.

No real Dhan creds, no database: the Dhan client is a recording fake and the DB
upsert helper is monkeypatched to capture rows. Verifies (per the F&O handoff
invariants) that live fetches refuse to run during market hours, that the MCX
segment/instrument codes are sent, and that the token comes from the
token-manager path (read_current_token → MasterTokenManager), never the static
.env token.
"""
import asyncio
from datetime import date, datetime, timezone

import pytest

from core.fno_backfill import _IST as IST  # reuse the canonical IST

from core import mcx_backfill as mb


# ── segment / instrument constants ───────────────────────────────────────────────
def test_mcx_segment_and_instrument_codes():
    assert mb.MCX_EXCHANGE_SEGMENT == "MCX_COMM"
    assert mb.MCX_INSTRUMENT == "FUTCOM"


# ── pure history parsing ──────────────────────────────────────────────────────────
def test_parse_mcx_history_attaches_security_id_and_oi():
    raw = {
        "data": {
            "timestamp": [1718000000, 1718086400],
            "open": [5500, 5510], "high": [5550, 5560],
            "low": [5480, 5490], "close": [5520, 5530],
            "volume": [1000, 1200], "open_interest": [9000, 9100],
        }
    }
    rows = mb.parse_mcx_history(raw, "CRUDEOILM", "111111", "1d", date(2026, 6, 30))
    assert len(rows) == 2
    r = rows[0]
    assert r["symbol"] == "CRUDEOILM"
    assert r["security_id"] == "111111"
    assert r["timeframe"] == "1d"
    assert r["open"] == 5500.0
    assert r["open_interest"] == 9000
    assert r["expiry_date"] == date(2026, 6, 30)
    assert r["time"].tzinfo is timezone.utc


def test_parse_mcx_history_empty():
    assert mb.parse_mcx_history({"data": {"timestamp": []}}, "GOLDM", "222") == []
    assert mb.parse_mcx_history({}, "GOLDM", "222") == []


# ── checkpointing ──────────────────────────────────────────────────────────────────
def test_checkpoint_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "mcx")
    assert mb.read_checkpoint("333", "1d") is None
    mb.write_checkpoint("333", "1d", {"rows": 42, "symbol": "NATURALGAS"})
    cp = mb.read_checkpoint("333", "1d")
    assert cp["rows"] == 42 and cp["symbol"] == "NATURALGAS"


def test_read_checkpoint_unreadable_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "mcx")
    (tmp_path / "mcx").mkdir(parents=True)
    p = mb._checkpoint_path("444", "1d")
    p.write_text("{ not json", encoding="utf-8")
    assert mb.read_checkpoint("444", "1d") is None


# ── orchestration: fake client + captured upsert ──────────────────────────────────
class _FakeClient:
    def __init__(self):
        self.calls = []

    async def get_daily_historical(self, **kw):
        self.calls.append(("daily", kw))
        return {"data": {"timestamp": [1718000000], "open": [5500], "high": [5550],
                         "low": [5480], "close": [5520], "volume": [1000],
                         "open_interest": [9000]}}

    async def get_intraday_historical(self, **kw):
        self.calls.append(("intraday", kw))
        return {"data": {"timestamp": [1718000000], "open": [5500], "high": [5550],
                         "low": [5480], "close": [5520], "volume": [1000],
                         "open_interest": [9000]}}


def _capture(store):
    def _fn(rows):
        store["rows"] = rows
        return len(rows)
    return _fn


def test_backfill_mcx_bars_daily_off_hours(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(mb, "_upsert_mcx_bars", _capture(captured))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "CRUDEOILM", "111111", "2025-01-01", "2026-06-18", now=now
        )
    )
    assert n == 1
    assert client.calls[0][0] == "daily"
    kw = client.calls[0][1]
    assert kw["exchange_segment"] == "MCX_COMM"
    assert kw["instrument"] == "FUTCOM"
    assert kw["security_id"] == "111111"
    row = captured["rows"][0]
    assert row["symbol"] == "CRUDEOILM"
    assert row["security_id"] == "111111"
    # checkpoint was written
    cp = mb.read_checkpoint("111111", "1d")
    assert cp is not None and cp["rows"] == 1


def test_backfill_mcx_bars_intraday_uses_interval(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(mb, "_upsert_mcx_bars", _capture(captured))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "GOLDM", "222222", "2026-06-01", "2026-06-18",
            timeframe="5min", interval="5", now=now,
        )
    )
    assert n == 1
    assert client.calls[0][0] == "intraday"
    kw = client.calls[0][1]
    assert kw["exchange_segment"] == "MCX_COMM"
    assert kw["instrument"] == "FUTCOM"
    assert kw["interval"] == "5"
    assert captured["rows"][0]["timeframe"] == "5min"


def test_backfill_mcx_bars_refuses_in_market_hours(monkeypatch):
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: 1 / 0)  # must never run
    client = _FakeClient()
    now = datetime(2026, 6, 15, 11, 0, tzinfo=IST)
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(mb.backfill_mcx_bars(client, "CRUDEOILM", "1", "a", "b", now=now))
    assert client.calls == []


# ── CLI arg-parsing ────────────────────────────────────────────────────────────────
def test_cli_parses_required_args():
    p = mb._build_arg_parser()
    args = p.parse_args(
        ["--symbol", "CRUDEOILM", "--security-id", "111111",
         "--from", "2025-01-01", "--to", "2026-06-18"]
    )
    assert args.symbol == "CRUDEOILM"
    assert args.security_id == "111111"
    assert args.from_date == "2025-01-01"
    assert args.to_date == "2026-06-18"
    assert args.timeframe == "1d"      # default
    assert args.interval is None       # default → daily endpoint


def test_cli_parses_intraday_and_expiry():
    p = mb._build_arg_parser()
    args = p.parse_args(
        ["--symbol", "GOLDM", "--security-id", "2", "--from", "a", "--to", "b",
         "--timeframe", "5min", "--interval", "5", "--expiry", "2026-06-30"]
    )
    assert args.timeframe == "5min"
    assert args.interval == "5"
    assert mb._parse_cli_expiry(args.expiry) == date(2026, 6, 30)


def test_cli_requires_symbol_and_security_id():
    p = mb._build_arg_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--from", "a", "--to", "b"])  # missing --symbol/--security-id


def test_parse_cli_expiry_invalid_raises():
    with pytest.raises(SystemExit):
        mb._parse_cli_expiry("30-06-2026")


# ── token path: managed token, NOT the static env token ───────────────────────────
def test_resolve_access_token_is_the_fno_helper():
    """mcx_backfill reuses the F&O resolve_access_token (live cache → PIN/TOTP)."""
    from core import fno_backfill
    assert mb.resolve_access_token is fno_backfill.resolve_access_token


def test_resolve_access_token_prefers_cache():
    from unittest.mock import MagicMock, patch

    mock_read = MagicMock(return_value="cached-tok")
    mock_mgr_cls = MagicMock()
    with patch("core.token_manager.read_current_token", mock_read), \
         patch("core.token_manager.MasterTokenManager", mock_mgr_cls):
        tok = asyncio.run(mb.resolve_access_token())
    assert tok == "cached-tok"
    mock_read.assert_called_once()
    mock_mgr_cls.assert_not_called()


def test_amain_uses_token_manager_not_static_env_token():
    """_amain must build DhanClient with the token-manager token, NOT
    cfg.dhan_access_token, and call backfill_mcx_bars."""
    import argparse
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    args = argparse.Namespace(
        symbol="CRUDEOILM", security_id="111111", from_date="2025-01-01",
        to_date="2026-06-18", timeframe="1d", interval=None, expiry=None,
    )

    mock_cfg = MagicMock()
    mock_cfg.db_url = "postgresql://fake/db"
    mock_cfg.dhan_client_id = "CLIENT1"
    mock_cfg.dhan_access_token = "STATIC-ENV-TOKEN-EXPIRED"

    mock_client_cls = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_resolve = AsyncMock(return_value="MANAGED-TOKEN")
    mock_backfill = AsyncMock(return_value=5)

    with patch.dict(sys.modules, {
        "config": MagicMock(get_config=MagicMock(return_value=mock_cfg)),
        "db": MagicMock(init_db=MagicMock()),
        "core.client": MagicMock(DhanClient=mock_client_cls),
    }), \
        patch.object(mb, "resolve_access_token", mock_resolve), \
        patch.object(mb, "backfill_mcx_bars", mock_backfill):
        asyncio.run(mb._amain(args))

    mock_resolve.assert_awaited_once()
    mock_backfill.assert_awaited_once()
    pos, kw = mock_client_cls.call_args
    passed = list(pos) + list(kw.values())
    assert "MANAGED-TOKEN" in passed
    assert "STATIC-ENV-TOKEN-EXPIRED" not in passed
