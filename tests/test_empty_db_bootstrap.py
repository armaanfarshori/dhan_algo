"""Fresh-DB bootstrap guards — AWS -> local home-server move.

The platform used to run against an AWS DB carrying years of history. It now
runs against a Docker TimescaleDB (docker-compose.yml) that starts **completely
empty**: no bars, no instruments, no trades. Two invariants have to hold for
that to work, and both are cheap to regress:

  1. `alembic upgrade head` must walk 001 -> 014 on a virgin database. That
     needs one root, one head, an unbroken chain, and the timescaledb extension
     created before the first hypertable.
  2. The PAPER boot path must DEGRADE, not crash, when every table is empty.
     Empty results are the expected steady state on a fresh box — the engine is
     supposed to log warnings, select nothing, and idle.

Neither half needs Docker or a live database.

[[ci-alembic-namespace-shadow]] — the repo-local ``alembic/`` directory (no
__init__.py) shadows the installed ``alembic`` package whenever repo-root leads
sys.path, as it does on CI. The migration checks below therefore parse the
files with ``ast``/text and never import ``alembic``, so they are immune to it.
See tests/test_scalper_migration.py for the stub-based alternative.
"""
from __future__ import annotations

import ast
import asyncio
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERSIONS_DIR = os.path.join(_REPO_ROOT, "alembic", "versions")


# ── Migration-graph helpers (no alembic import — see module docstring) ────────

def _revision_graph() -> dict[str, dict]:
    """Map revision id -> {down, filename, funcs} by parsing the version files.

    Handles both annotated (`revision: str = "014"`) and plain
    (`revision = "005"`) assignment styles, which the repo mixes.
    """
    graph: dict[str, dict] = {}
    for name in sorted(os.listdir(_VERSIONS_DIR)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(_VERSIONS_DIR, name)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        rev = down = None
        sentinel = object()
        down_seen = False
        funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}

        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
                value = node.value
            else:
                continue
            resolved = value.value if isinstance(value, ast.Constant) else sentinel
            for target in targets:
                if target == "revision" and resolved is not sentinel:
                    rev = resolved
                elif target == "down_revision":
                    down_seen = True
                    down = resolved if resolved is not sentinel else None

        assert rev is not None, f"{name}: no literal `revision` assignment found"
        assert down_seen, f"{name}: no `down_revision` assignment found"
        assert rev not in graph, f"duplicate revision id {rev!r} in {name}"
        graph[rev] = {"down": down, "filename": name, "funcs": funcs}
    return graph


def test_migration_graph_is_a_single_unbroken_chain():
    """One root, one head, every revision reachable — `upgrade head` from zero.

    A second root or a fork would make `alembic upgrade head` ambiguous or
    leave revisions stranded, which only ever shows up on a virgin DB.
    """
    graph = _revision_graph()
    assert len(graph) >= 14, "expected the 001..014 chain to be present"

    roots = [rev for rev, meta in graph.items() if meta["down"] is None]
    assert roots == ["001"], f"expected exactly one root revision '001', got {roots}"

    downs = {meta["down"] for meta in graph.values()}
    heads = sorted(rev for rev in graph if rev not in downs)
    assert len(heads) == 1, f"expected exactly one head, got {heads}"

    # Walk root -> head; every step must have exactly one successor.
    order = []
    current = roots[0]
    while current is not None:
        order.append(current)
        successors = [rev for rev, meta in graph.items() if meta["down"] == current]
        assert len(successors) <= 1, f"revision graph forks at {current}: {successors}"
        current = successors[0] if successors else None

    assert order[-1] == heads[0]
    assert len(order) == len(graph), (
        "unreachable revisions: "
        f"{sorted(set(graph) - set(order))} — the chain does not cover every file"
    )
    # Every dangling parent must actually exist.
    for rev, meta in graph.items():
        if meta["down"] is not None:
            assert meta["down"] in graph, (
                f"{meta['filename']}: down_revision {meta['down']!r} does not exist"
            )


def test_every_migration_defines_upgrade_and_downgrade():
    for rev, meta in _revision_graph().items():
        assert "upgrade" in meta["funcs"], f"{meta['filename']}: missing upgrade()"
        assert "downgrade" in meta["funcs"], f"{meta['filename']}: missing downgrade()"


def test_root_migration_creates_timescaledb_extension_before_any_hypertable():
    """A virgin container has no timescaledb extension in the target database.

    001 must issue CREATE EXTENSION before the first create_hypertable() call,
    otherwise the very first migration fails on a fresh volume.
    """
    graph = _revision_graph()
    root = next(rev for rev, meta in graph.items() if meta["down"] is None)
    path = os.path.join(_VERSIONS_DIR, graph[root]["filename"])
    with open(path, encoding="utf-8") as fh:
        source = fh.read()

    ext_at = source.find("CREATE EXTENSION IF NOT EXISTS timescaledb")
    assert ext_at != -1, (
        "root migration must run `CREATE EXTENSION IF NOT EXISTS timescaledb` — "
        "a fresh TimescaleDB container has no extension in a new database"
    )
    hypertable_at = source.find("create_hypertable")
    if hypertable_at != -1:
        assert ext_at < hypertable_at, (
            "CREATE EXTENSION must precede the first create_hypertable() call"
        )


# ── Zero-history paper path: empty tables must degrade, not crash ─────────────

@contextmanager
def _session_returning(**results):
    """Mock session whose execute() returns the given scalar()/fetchall()."""
    session = MagicMock()
    if "scalar" in results:
        session.execute.return_value.scalar.return_value = results["scalar"]
    if "fetchall" in results:
        session.execute.return_value.fetchall.return_value = results["fetchall"]
    yield session


def _make_risk_engine() -> RiskEngine:
    params = RiskParams(
        equity_base=500_000,
        risk_per_trade=0.01,          # ₹5,000 budget/trade
        max_daily_loss_pct=0.02,
        weekly_loss_pct=0.05,
        max_notional_pct=0.20,
        max_gross_exposure_pct=1.00,
        adv_participation_pct=0.01,
        min_stop_distance_pct=0.0035,
    )
    return RiskEngine(params, Portfolio(mode="PAPER"), ltp_lookup=lambda sid: 0.0)


def test_get_adv_reads_through_the_mocked_session():
    """Positive control: proves the db.get_session patch actually intercepts.

    Without this, the empty-table test below would pass vacuously — the real
    (unpatched) code path also returns None, via the exception handler, when no
    database is configured. Asserting a real value flows through pins the
    harness as live, so `None` in the next test means "empty table", not
    "mock never ran".
    """
    risk = _make_risk_engine()
    with patch("db.get_session", lambda: _session_returning(scalar=123_456.0)):
        assert asyncio.run(risk.get_adv("2885")) == 123_456.0


def test_get_adv_returns_none_when_bars_is_empty():
    """AVG(volume) over zero rows is SQL NULL -> None, never float(None).

    On a fresh box `bars` is empty for every security, so this is the ONLY
    path get_adv takes until a backfill runs.
    """
    risk = _make_risk_engine()
    with patch("db.get_session", lambda: _session_returning(scalar=None)):
        adv = asyncio.run(risk.get_adv("2885"))
    assert adv is None


def test_size_position_skips_liquidity_cap_when_adv_is_none():
    """With no volume history the ADV cap drops out; sizing still works.

    The cap is multiplicative, never a divisor — an empty `bars` table must not
    size every trade to zero (nor raise).
    """
    risk = _make_risk_engine()
    qty = risk.size_position(entry=100.0, stop=98.0, adv=None)
    assert qty > 0
    # Same inputs with a real ADV clamp to the participation limit, proving the
    # None branch is a skipped cap rather than an accidental no-op everywhere.
    assert risk.size_position(entry=100.0, stop=98.0, adv=10_000.0) == 100


def test_get_adv_survives_db_failure():
    """A missing/erroring bars table must not take the runner down."""
    risk = _make_risk_engine()

    def _boom():
        raise RuntimeError("relation \"bars\" does not exist")

    with patch("db.get_session", _boom):
        assert asyncio.run(risk.get_adv("2885")) is None


def test_reconcile_on_boot_reads_through_the_mocked_session():
    """Positive control for the reconcile harness — see get_adv's control.

    An unpatched reconcile_on_boot also ends up with an empty portfolio (via
    its except branch), so the empty-table assertion below only means something
    once a row is shown to make it through.
    """
    import datetime
    from zoneinfo import ZoneInfo

    today = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    portfolio = Portfolio(mode="PAPER")
    row = [("2885", today, 10, 100.0, "ORB")]
    with patch("db.get_session", lambda: _session_returning(fetchall=row)):
        asyncio.run(portfolio.reconcile_on_boot())
    assert [p.security_id for p in portfolio.open_positions()] == ["2885"]


def test_reconcile_on_boot_with_empty_engine_positions():
    """Fresh DB -> zero rows -> empty portfolio, no exception."""
    portfolio = Portfolio(mode="PAPER")
    with patch("db.get_session", lambda: _session_returning(fetchall=[])):
        asyncio.run(portfolio.reconcile_on_boot())
    assert portfolio.open_positions() == []


def test_reconcile_on_boot_survives_missing_table():
    portfolio = Portfolio(mode="PAPER")

    def _boom():
        raise RuntimeError("relation \"engine_positions\" does not exist")

    with patch("db.get_session", _boom):
        asyncio.run(portfolio.reconcile_on_boot())
    assert portfolio.open_positions() == []


def test_watchlist_refresh_without_scrip_master_yields_empty_list(tmp_path, monkeypatch):
    """No cached scrip master (fresh box) -> empty fallback, logged not raised.

    trader.py falls back to this list when the screener returns nothing, so it
    must survive the state where neither source has data yet.
    """
    import core.watchlist as watchlist_mod

    monkeypatch.setattr(watchlist_mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(watchlist_mod, "CACHE_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(watchlist_mod, "MASTER_CSV", tmp_path / "absent.csv")

    manager = watchlist_mod.WatchlistManager()
    manager._load_symbol_index()          # missing CSV -> warns, returns
    asyncio.run(manager.refresh())

    assert manager.get() == []
    assert manager.get_security_ids() == []
    # trader.py slices this list; slicing an empty list must stay empty.
    assert manager.get()[:20] == []


@pytest.mark.parametrize("high,low", [(0.0, float("inf")), (0.0, 0.0), (90.0, 100.0)])
def test_orb_seed_rejects_empty_rest_response_sentinels(high, low):
    """seed_opening_ranges() leaves (0.0, inf) when the REST reply has no bars.

    Those sentinels must not lock a bogus opening range — that would hand every
    reconciled position a nonsense stop.
    """
    from strategies.orb import ORB
    import datetime as _dt

    orb = ORB("999")
    orb.seed_opening_range(_dt.date.today(), high, low)
    assert not orb.or_locked
