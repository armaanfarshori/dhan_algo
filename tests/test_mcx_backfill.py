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


# ── intraday 90-day windowing (DH-905 fix) ─────────────────────────────────────────
def test_intraday_windows_single_window_under_90_days():
    w = mb._intraday_windows("2026-06-01", "2026-06-18")
    assert w == [("2026-06-01", "2026-06-18")]


def test_intraday_windows_exactly_90_days_is_one_window():
    # 2026-01-01 .. 2026-03-31 inclusive = 90 distinct days → single window.
    w = mb._intraday_windows("2026-01-01", "2026-03-31")
    assert len(w) == 1
    assert w[0] == ("2026-01-01", "2026-03-31")


def test_intraday_windows_over_90_days_splits_with_no_gaps_or_overlaps():
    from datetime import date as _date, datetime as _dt, timedelta as _td

    from_d, to_d = "2025-01-01", "2026-06-18"
    w = mb._intraday_windows(from_d, to_d)
    assert len(w) > 1
    # First window starts at from_date; last window ends at to_date.
    assert w[0][0] == from_d
    assert w[-1][1] == to_d
    # Every window is ≤90 days and contiguous (next.from == prev.to + 1 day),
    # with no overlap and no gap.
    for (f, t) in w:
        fd = _dt.strptime(f, "%Y-%m-%d").date()
        td = _dt.strptime(t, "%Y-%m-%d").date()
        assert fd <= td
        assert (td - fd).days + 1 <= mb._MAX_INTRADAY_CHUNK
    for prev, nxt in zip(w, w[1:]):
        prev_to = _dt.strptime(prev[1], "%Y-%m-%d").date()
        nxt_from = _dt.strptime(nxt[0], "%Y-%m-%d").date()
        assert nxt_from == prev_to + _td(days=1)
    # Full span is covered exactly once.
    assert _dt.strptime(w[0][0], "%Y-%m-%d").date() == _date(2025, 1, 1)
    assert _dt.strptime(w[-1][1], "%Y-%m-%d").date() == _date(2026, 6, 18)


def test_intraday_windows_inverted_range_raises():
    with pytest.raises(ValueError):
        mb._intraday_windows("2026-06-18", "2026-01-01")


def test_backfill_mcx_bars_intraday_windows_long_range(monkeypatch, tmp_path):
    """A >90-day intraday range fans out into multiple windowed intraday calls
    covering the full span with ≤90-day bounds; row counts sum across windows."""
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: len(rows))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    # No real sleeping in tests.
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(mb.asyncio, "sleep", _no_sleep)

    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "GOLDM", "222222", "2025-01-01", "2026-06-18",
            timeframe="5min", interval="5", now=now,
        )
    )
    intraday_calls = [c for c in client.calls if c[0] == "intraday"]
    expected_windows = mb._intraday_windows("2025-01-01", "2026-06-18")
    assert len(intraday_calls) == len(expected_windows) > 1
    # Each call carried the right segment/instrument/interval + ≤90-day bounds.
    seen = []
    for (_, kw) in intraday_calls:
        assert kw["exchange_segment"] == "MCX_COMM"
        assert kw["instrument"] == "FUTCOM"
        assert kw["interval"] == "5"
        seen.append((kw["from_date"], kw["to_date"]))
    assert seen == expected_windows  # exact span coverage, in order
    # Each FakeClient call yields 1 row → summed across windows.
    assert n == len(expected_windows)
    # Checkpoint advanced to the final window end, total rows recorded.
    cp = mb.read_checkpoint("222222", "5min")
    assert cp["to_date"] == "2026-06-18"
    assert cp["rows"] == n


def test_backfill_mcx_bars_intraday_resumes_from_checkpoint(monkeypatch, tmp_path):
    """A pre-existing checkpoint (matching from_date, partial to_date) makes the
    next run fetch ONLY the remaining windows (after cp["to_date"]) and return a
    total that includes the carried-forward checkpoint rows."""
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: len(rows))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(mb.asyncio, "sleep", _no_sleep)

    from_d, to_d = "2025-01-01", "2026-06-18"
    all_windows = mb._intraday_windows(from_d, to_d)
    assert len(all_windows) > 2  # need a partial checkpoint mid-range
    # Pretend the first two windows already completed, carrying 7 rows.
    cp_to = all_windows[1][1]
    mb.write_checkpoint(
        "222222", "5min",
        {"symbol": "GOLDM", "security_id": "222222", "from_date": from_d,
         "to_date": cp_to, "timeframe": "5min", "rows": 7},
    )

    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "GOLDM", "222222", from_d, to_d,
            timeframe="5min", interval="5", now=now,
        )
    )
    # Only the windows AFTER the checkpoint should be fetched.
    from datetime import date as _date, timedelta as _td
    resume_from = (_date.fromisoformat(cp_to) + _td(days=1)).strftime("%Y-%m-%d")
    remaining = mb._intraday_windows(resume_from, to_d)
    intraday_calls = [c for c in client.calls if c[0] == "intraday"]
    assert len(intraday_calls) == len(remaining)
    seen = [(kw["from_date"], kw["to_date"]) for (_, kw) in intraday_calls]
    assert seen == remaining
    # Total = carried 7 + 1 row per remaining window.
    assert n == 7 + len(remaining)
    # Checkpoint advanced to the requested end.
    assert mb.read_checkpoint("222222", "5min")["to_date"] == to_d


def test_backfill_mcx_bars_intraday_mismatched_checkpoint_full_refetch(monkeypatch, tmp_path):
    """A checkpoint whose from_date differs from the requested from_date is
    ignored → the full range is re-fetched and rows are NOT carried forward."""
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: len(rows))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(mb.asyncio, "sleep", _no_sleep)

    from_d, to_d = "2025-01-01", "2026-06-18"
    all_windows = mb._intraday_windows(from_d, to_d)
    # Checkpoint for a DIFFERENT requested from_date — must be ignored.
    mb.write_checkpoint(
        "222222", "5min",
        {"symbol": "GOLDM", "security_id": "222222", "from_date": "2024-01-01",
         "to_date": all_windows[1][1], "timeframe": "5min", "rows": 999},
    )

    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "GOLDM", "222222", from_d, to_d,
            timeframe="5min", interval="5", now=now,
        )
    )
    intraday_calls = [c for c in client.calls if c[0] == "intraday"]
    assert len(intraday_calls) == len(all_windows)  # full re-fetch
    seen = [(kw["from_date"], kw["to_date"]) for (_, kw) in intraday_calls]
    assert seen == all_windows
    assert n == len(all_windows)  # no carried-forward 999


def test_backfill_mcx_bars_intraday_single_day(monkeypatch, tmp_path):
    """A single-day (from==to) intraday range = exactly one window, one fetch."""
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: len(rows))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(mb.asyncio, "sleep", _no_sleep)

    client = _FakeClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "GOLDM", "222222", "2026-06-10", "2026-06-10",
            timeframe="5min", interval="5", now=now,
        )
    )
    intraday_calls = [c for c in client.calls if c[0] == "intraday"]
    assert len(intraday_calls) == 1
    _, kw = intraday_calls[0]
    assert kw["from_date"] == "2026-06-10"
    assert kw["to_date"] == "2026-06-10"
    assert n == 1


def test_backfill_mcx_bars_intraday_retries_on_dh904(monkeypatch, tmp_path):
    """A DH-904 on a window is retried (with backoff) rather than dropped, on a
    MULTI-window range — and the loop proceeds to the next window after retry."""
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: len(rows))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    sleeps: list = []
    async def _rec_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(mb.asyncio, "sleep", _rec_sleep)

    # >90 days → multiple windows.
    from_d, to_d = "2025-01-01", "2026-06-18"
    expected_windows = mb._intraday_windows(from_d, to_d)
    assert len(expected_windows) > 1

    class _FlakyClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def get_intraday_historical(self, **kw):
            self.attempts += 1
            # DH-904 only on the very first attempt of the first window.
            if self.attempts == 1:
                raise RuntimeError("DH-904: Too many requests")
            return await super().get_intraday_historical(**kw)

    client = _FlakyClient()
    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    n = asyncio.run(
        mb.backfill_mcx_bars(
            client, "GOLDM", "222222", from_d, to_d,
            timeframe="5min", interval="5", now=now,
        )
    )
    # First window: 1 DH-904 + 1 success = 2 attempts; remaining windows: 1 each.
    assert client.attempts == len(expected_windows) + 1
    # The loop still covered every window (one successful intraday call each).
    intraday_calls = [c for c in client.calls if c[0] == "intraday"]
    assert len(intraday_calls) == len(expected_windows)
    seen = [(kw["from_date"], kw["to_date"]) for (_, kw) in intraday_calls]
    assert seen == expected_windows
    assert n == len(expected_windows)
    assert mb._DH904_RETRY_WAIT[0] in sleeps  # backed off


def test_backfill_mcx_bars_intraday_non_dh904_propagates(monkeypatch, tmp_path):
    """A non-DH-904 error is NOT swallowed by the retry loop."""
    monkeypatch.setattr(mb, "_upsert_mcx_bars", lambda rows: len(rows))
    monkeypatch.setattr(mb, "CHECKPOINT_DIR", tmp_path / "cp")
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(mb.asyncio, "sleep", _no_sleep)

    class _BoomClient(_FakeClient):
        async def get_intraday_historical(self, **kw):
            raise RuntimeError("DH-905: boom")

    now = datetime(2026, 6, 20, 18, 0, tzinfo=IST)
    with pytest.raises(RuntimeError, match="DH-905"):
        asyncio.run(
            mb.backfill_mcx_bars(
                _BoomClient(), "GOLDM", "222222", "2026-06-01", "2026-06-18",
                timeframe="5min", interval="5", now=now,
            )
        )


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
