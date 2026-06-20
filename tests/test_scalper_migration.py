"""Smoke test for the 014 scalper-tables migration.

Guards two things:
  1. The migration file imports cleanly and chains onto the previous head (013),
     so `alembic upgrade head` won't fail on a broken revision graph.
  2. upgrade()/downgrade() call op.create_table / op.create_index for both
     scalper tables (a structural sanity check without a live DB).

[[ci-alembic-namespace-shadow]] — the repo's local ``alembic/`` package dir (no
__init__.py) shadows the installed ``alembic`` package when repo-root is first on
sys.path (as on CI). Importing a migration then fails with
``cannot import name 'op' from 'alembic'``. We defend by stubbing ``alembic`` +
``alembic.op`` in sys.modules BEFORE importing the migration module, exactly as
the memory prescribes.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "alembic", "versions", "014_scalper_tables.py",
)


def _load_migration_with_stubbed_op():
    """Import the 014 migration with a stubbed alembic.op (returns module, op_mock)."""
    op_mock = MagicMock(name="alembic.op")

    # Build a fake `alembic` package exposing `op` so `from alembic import op` works
    # regardless of whether the real package or the repo-local dir is on sys.path.
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = op_mock

    saved_alembic = sys.modules.get("alembic")
    saved_alembic_op = sys.modules.get("alembic.op")
    sys.modules["alembic"] = fake_alembic
    sys.modules["alembic.op"] = op_mock
    # Drop a previously-loaded copy so our stub takes effect on (re)import.
    sys.modules.pop("scalper_migration_014", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "scalper_migration_014", _MIGRATION_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved_alembic is not None:
            sys.modules["alembic"] = saved_alembic
        else:
            sys.modules.pop("alembic", None)
        if saved_alembic_op is not None:
            sys.modules["alembic.op"] = saved_alembic_op
        else:
            sys.modules.pop("alembic.op", None)
    return mod, op_mock


@pytest.mark.skipif(not os.path.exists(_MIGRATION_PATH), reason="014 migration not present")
def test_migration_revision_chain():
    mod, _ = _load_migration_with_stubbed_op()
    assert mod.revision == "014"
    assert mod.down_revision == "013"


@pytest.mark.skipif(not os.path.exists(_MIGRATION_PATH), reason="014 migration not present")
def test_upgrade_creates_both_tables():
    mod, op_mock = _load_migration_with_stubbed_op()
    mod.upgrade()
    created = [c.args[0] for c in op_mock.create_table.call_args_list]
    assert "scalper_trades" in created
    assert "scalper_governor" in created
    # at least one index per table
    assert op_mock.create_index.call_count >= 2


@pytest.mark.skipif(not os.path.exists(_MIGRATION_PATH), reason="014 migration not present")
def test_downgrade_drops_both_tables():
    mod, op_mock = _load_migration_with_stubbed_op()
    mod.downgrade()
    dropped_sql = " ".join(
        str(c.args[0]) for c in op_mock.execute.call_args_list
    )
    assert "scalper_trades" in dropped_sql
    assert "scalper_governor" in dropped_sql
