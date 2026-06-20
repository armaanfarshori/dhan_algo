"""Offline sanity for alembic/versions/012_mcx_foundation.py.

No DB: this only imports the migration module and asserts its revision linkage,
that upgrade()/downgrade() are callable, and — by driving a recording fake
``op`` — that upgrade creates the ``mcx_bars`` hypertable with the expected
PK/index and downgrade drops only that table. Additive: it must never reference
any pre-existing table.

CI-robustness note: the repo root carries a local ``alembic/`` directory (no
``__init__.py``). With the repo root first on ``sys.path`` (as on CI), that
directory shadows the INSTALLED ``alembic`` package as a namespace package with
NO ``op`` attribute, so ``from alembic import op`` raises
``ImportError: cannot import name 'op' from 'alembic'`` at migration import
time. To stay independent of which ``alembic`` wins, we inject a fake
``alembic`` module exposing a recording ``op`` into ``sys.modules`` BEFORE
exec'ing the migration, then restore the real modules on teardown so no other
test is affected. ``sqlalchemy`` stays real — the PK/column assertions inspect
genuine ``sa.Column``/``sa.PrimaryKeyConstraint`` objects.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_MIG = Path(__file__).parent.parent / "alembic" / "versions" / "012_mcx_foundation.py"


@pytest.fixture
def loaded_migration():
    """Load 012 with a fake ``alembic.op`` injected, regardless of sys.path.

    Yields ``(module, fake_op)``. The fake ``op`` is a MagicMock so the test
    can attach ``side_effect`` recorders and assert on the calls the migration
    makes. Real ``alembic``/``alembic.op`` modules are restored on teardown.
    """
    saved = {k: sys.modules.get(k) for k in ("alembic", "alembic.op")}

    fake_op = MagicMock(name="alembic.op")
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = fake_op
    sys.modules["alembic"] = fake_alembic
    sys.modules["alembic.op"] = fake_op

    try:
        spec = importlib.util.spec_from_file_location("mig_012_mcx", _MIG)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # The migration did ``from alembic import op`` → mod.op is our fake.
        assert mod.op is fake_op
        yield mod, fake_op
    finally:
        sys.modules.pop("mig_012_mcx", None)
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def test_revision_linkage(loaded_migration):
    mod, _ = loaded_migration
    assert mod.revision == "012"
    assert mod.down_revision == "011"


def test_upgrade_creates_mcx_bars_hypertable(loaded_migration):
    mod, fake_op = loaded_migration
    create_calls = []
    execute_sql = []

    fake_op.create_table.side_effect = lambda name, *cols, **kw: create_calls.append((name, cols))
    fake_op.execute.side_effect = lambda sql: execute_sql.append(sql)

    mod.upgrade()

    # Exactly one table created — mcx_bars (additive; touches nothing else)
    assert len(create_calls) == 1
    assert create_calls[0][0] == "mcx_bars"

    joined = " ".join(execute_sql)
    assert "create_hypertable('mcx_bars', 'time'" in joined
    assert "ix_mcx_bars_symbol_tf_time" in joined
    assert "mcx_bars (symbol, timeframe, time DESC)" in joined


def test_upgrade_pk_mirrors_index_bars(loaded_migration):
    """PK must be (security_id, timeframe, time) like index_bars."""
    mod, fake_op = loaded_migration
    captured = {}

    def _create_table(name, *cols, **kw):
        captured["cols"] = cols

    fake_op.create_table.side_effect = _create_table
    mod.upgrade()

    pk = [c for c in captured["cols"] if c.__class__.__name__ == "PrimaryKeyConstraint"]
    assert pk, "no PrimaryKeyConstraint in mcx_bars"
    # Unbound constraints keep their column names in _pending_colargs.
    names = [getattr(c, "name", c) for c in pk[0]._pending_colargs]
    assert names == ["security_id", "timeframe", "time"]

    # Required column names present
    colnames = {getattr(c, "name", None) for c in captured["cols"]}
    for required in ("time", "security_id", "symbol", "expiry_date", "timeframe",
                     "open", "high", "low", "close", "volume", "open_interest"):
        assert required in colnames, f"missing column {required}"


def test_downgrade_drops_only_mcx_bars(loaded_migration):
    mod, fake_op = loaded_migration
    execute_sql = []
    fake_op.execute.side_effect = lambda sql: execute_sql.append(sql)

    mod.downgrade()

    assert len(execute_sql) == 1
    assert "DROP TABLE IF EXISTS mcx_bars" in execute_sql[0]


@pytest.mark.parametrize("forbidden", [
    "bars", "ticks", "index_bars", "futures_bars", "option_chain_snapshot",
    "fno_instruments", "fno_paper_trades", "signals", "trades",
])
def test_migration_does_not_touch_existing_tables(forbidden):
    """The migration source must not reference any pre-existing table name
    (additive-only guarantee)."""
    src = _MIG.read_text(encoding="utf-8")
    # Allow the names to appear only inside prose/comments comparing to index_bars,
    # but never inside an op.create_table/op.execute statement. Crude but effective:
    # assert no SQL/DDL verb adjacent to a forbidden table name.
    for verb in ("CREATE TABLE", "DROP TABLE", "ALTER TABLE", 'create_table("'):
        assert f"{verb} {forbidden}" not in src
        assert f'create_table("{forbidden}"' not in src
