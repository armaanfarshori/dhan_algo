"""/api/backtest/* handlers — list runs, fetch one, 404, path-traversal guard."""
import asyncio
import json

from aiohttp.test_utils import make_mocked_request

from apps.routes import backtest as bt

_SAMPLE = {
    "label": "ORB standalone",
    "survivorship": "CEILING",
    "provenance": {"generated_at": "2026-06-17T00:00:00Z", "git_sha": "abc123",
                   "params": {"gate": "none", "from": "2024-01-01", "to": "2026-01-01",
                              "split_date": "2025-07-01"}},
    "panel": {"full": {"sharpe_daily_ann": 1.2, "net_pnl": 12345.0, "trades": 200}},
}


def _json(resp):
    return json.loads(resp.text)


def test_runs_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(bt, "_RESULTS", tmp_path)
    resp = asyncio.run(bt.backtest_runs_handler(make_mocked_request("GET", "/api/backtest/runs")))
    assert _json(resp) == {"runs": [], "count": 0}


def test_runs_list_and_fetch(monkeypatch, tmp_path):
    (tmp_path / "M3-2026-06-17.json").write_text(json.dumps(_SAMPLE))
    monkeypatch.setattr(bt, "_RESULTS", tmp_path)

    runs = _json(asyncio.run(bt.backtest_runs_handler(make_mocked_request("GET", "/api/backtest/runs"))))
    assert runs["count"] == 1
    row = runs["runs"][0]
    assert row["name"] == "M3-2026-06-17"
    assert row["sharpe"] == 1.2 and row["gate"] == "none" and row["trades"] == 200
    assert row["survivorship"] == "CEILING"

    req = make_mocked_request("GET", "/api/backtest/runs/M3-2026-06-17",
                              match_info={"name": "M3-2026-06-17"})
    one = _json(asyncio.run(bt.backtest_run_handler(req)))
    assert one["panel"]["full"]["trades"] == 200


def test_run_404(monkeypatch, tmp_path):
    monkeypatch.setattr(bt, "_RESULTS", tmp_path)
    req = make_mocked_request("GET", "/api/backtest/runs/nope", match_info={"name": "nope"})
    resp = asyncio.run(bt.backtest_run_handler(req))
    assert resp.status == 404


def test_path_traversal_guarded(monkeypatch, tmp_path):
    monkeypatch.setattr(bt, "_RESULTS", tmp_path)
    req = make_mocked_request("GET", "/api/backtest/runs/x",
                              match_info={"name": "../../etc/passwd"})
    resp = asyncio.run(bt.backtest_run_handler(req))
    assert resp.status == 404      # stem-only → resolves to a non-existent file in _RESULTS
