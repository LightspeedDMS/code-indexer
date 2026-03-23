"""
Unit tests for UpgradeRegistryService.

Story #431: Rolling Upgrade with PostgreSQL Upgrade Registry

All tests mock the ConnectionPool — no real PostgreSQL required.
The mock hierarchy mirrors the pattern used in test_background_jobs_postgres.py:
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchone() / cur.fetchall()
    cur.rowcount
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from code_indexer.server.services.upgrade_registry_service import (
    UpgradeRegistryService,
    _row_to_dict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(fetchone=None, fetchall=None, rowcount=1):
    """
    Build a mock ConnectionPool whose context-manager chain returns a
    cursor pre-loaded with the given return values.

    Returns (pool, conn, cur) for inspection in tests.
    """
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


def _make_service(pool, node_id="node-1"):
    """Create an UpgradeRegistryService with the table already marked as ensured."""
    svc = UpgradeRegistryService(pool, node_id)
    svc._table_ensured = True  # skip DDL in unit tests
    return svc


# ---------------------------------------------------------------------------
# _row_to_dict
# ---------------------------------------------------------------------------


class TestRowToDict:
    def test_maps_all_columns(self):
        row = (
            42,
            "node-a",
            "9.0.0",
            "9.1.0",
            "completed",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T01:00:00+00:00",
            None,
        )
        result = _row_to_dict(row)
        assert result == {
            "id": 42,
            "node_id": "node-a",
            "version_from": "9.0.0",
            "version_to": "9.1.0",
            "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T01:00:00+00:00",
            "error_message": None,
        }

    def test_maps_error_message(self):
        row = (1, "n", "1.0", "2.0", "failed", "t1", "t2", "disk full")
        result = _row_to_dict(row)
        assert result["error_message"] == "disk full"
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# can_upgrade
# ---------------------------------------------------------------------------


class TestCanUpgrade:
    def test_returns_true_when_no_upgrading_row(self):
        pool, _, cur = _make_pool(fetchone=None)
        svc = _make_service(pool)
        assert svc.can_upgrade() is True

    def test_returns_false_when_another_node_upgrading(self):
        pool, _, cur = _make_pool(fetchone=("node-2",))
        svc = _make_service(pool)
        assert svc.can_upgrade() is False

    def test_executes_correct_sql(self):
        pool, _, cur = _make_pool(fetchone=None)
        svc = _make_service(pool)
        svc.can_upgrade()
        sql_called = cur.execute.call_args[0][0]
        assert "status = 'upgrading'" in sql_called
        assert "upgrade_registry" in sql_called


# ---------------------------------------------------------------------------
# begin_upgrade
# ---------------------------------------------------------------------------


class TestBeginUpgrade:
    def test_returns_true_on_successful_insert(self):
        pool, _, _ = _make_pool()
        svc = _make_service(pool)
        result = svc.begin_upgrade("9.0.0", "9.1.0")
        assert result is True

    def test_inserts_correct_values(self):
        pool, _, cur = _make_pool()
        svc = _make_service(pool, node_id="my-node")
        svc.begin_upgrade("1.0.0", "2.0.0")
        params = cur.execute.call_args[0][1]
        assert params == ("my-node", "1.0.0", "2.0.0")

    def test_returns_false_on_unique_constraint_violation(self):
        pool, conn, cur = _make_pool()
        # Simulate psycopg raising a unique-violation error
        cur.execute.side_effect = Exception(
            'duplicate key value violates unique constraint "upgrade_registry_one_upgrading"'
        )
        svc = _make_service(pool)
        result = svc.begin_upgrade("9.0.0", "9.1.0")
        assert result is False

    def test_returns_false_on_generic_unique_error(self):
        pool, _, cur = _make_pool()
        cur.execute.side_effect = Exception("UNIQUE constraint failed")
        svc = _make_service(pool)
        result = svc.begin_upgrade("9.0.0", "9.1.0")
        assert result is False

    def test_reraises_non_constraint_exceptions(self):
        pool, _, cur = _make_pool()
        cur.execute.side_effect = Exception("connection timeout")
        svc = _make_service(pool)
        with pytest.raises(Exception, match="connection timeout"):
            svc.begin_upgrade("9.0.0", "9.1.0")


# ---------------------------------------------------------------------------
# complete_upgrade
# ---------------------------------------------------------------------------


class TestCompleteUpgrade:
    def test_updates_status_to_completed(self):
        pool, _, cur = _make_pool(rowcount=1)
        svc = _make_service(pool)
        svc.complete_upgrade()  # should not raise
        sql_called = cur.execute.call_args[0][0]
        assert "status = 'completed'" in sql_called
        assert "upgrade_registry" in sql_called

    def test_passes_node_id_in_params(self):
        pool, _, cur = _make_pool(rowcount=1)
        svc = _make_service(pool, node_id="node-abc")
        svc.complete_upgrade()
        params = cur.execute.call_args[0][1]
        assert "node-abc" in params

    def test_raises_runtime_error_when_no_row_found(self):
        pool, _, cur = _make_pool(rowcount=0)
        svc = _make_service(pool)
        with pytest.raises(RuntimeError, match="No upgrading row found"):
            svc.complete_upgrade()


# ---------------------------------------------------------------------------
# fail_upgrade
# ---------------------------------------------------------------------------


class TestFailUpgrade:
    def test_updates_status_to_failed(self):
        pool, _, cur = _make_pool(rowcount=1)
        svc = _make_service(pool)
        svc.fail_upgrade("disk full")
        sql_called = cur.execute.call_args[0][0]
        assert "status = 'failed'" in sql_called
        assert "upgrade_registry" in sql_called

    def test_records_error_message_in_params(self):
        pool, _, cur = _make_pool(rowcount=1)
        svc = _make_service(pool, node_id="n1")
        svc.fail_upgrade("OOM error")
        params = cur.execute.call_args[0][1]
        assert "OOM error" in params
        assert "n1" in params

    def test_raises_runtime_error_when_no_row_found(self):
        pool, _, cur = _make_pool(rowcount=0)
        svc = _make_service(pool)
        with pytest.raises(RuntimeError, match="No upgrading row found"):
            svc.fail_upgrade("some error")


# ---------------------------------------------------------------------------
# get_upgrade_history
# ---------------------------------------------------------------------------


class TestGetUpgradeHistory:
    def test_returns_empty_list_when_no_rows(self):
        pool, _, _ = _make_pool(fetchall=[])
        svc = _make_service(pool)
        result = svc.get_upgrade_history()
        assert result == []

    def test_returns_mapped_dicts(self):
        rows = [
            (1, "n1", "1.0", "2.0", "completed", "t1", "t2", None),
            (2, "n2", "2.0", "3.0", "failed", "t3", "t4", "error"),
        ]
        pool, _, _ = _make_pool(fetchall=rows)
        svc = _make_service(pool)
        result = svc.get_upgrade_history()
        assert len(result) == 2
        assert result[0]["node_id"] == "n1"
        assert result[0]["status"] == "completed"
        assert result[1]["node_id"] == "n2"
        assert result[1]["error_message"] == "error"

    def test_passes_limit_to_query(self):
        pool, _, cur = _make_pool(fetchall=[])
        svc = _make_service(pool)
        svc.get_upgrade_history(limit=5)
        params = cur.execute.call_args[0][1]
        assert params == (5,)

    def test_default_limit_is_20(self):
        pool, _, cur = _make_pool(fetchall=[])
        svc = _make_service(pool)
        svc.get_upgrade_history()
        params = cur.execute.call_args[0][1]
        assert params == (20,)

    def test_orders_by_started_at_desc(self):
        pool, _, cur = _make_pool(fetchall=[])
        svc = _make_service(pool)
        svc.get_upgrade_history()
        sql_called = cur.execute.call_args[0][0]
        assert "ORDER BY started_at DESC" in sql_called


# ---------------------------------------------------------------------------
# get_current_upgrading_node
# ---------------------------------------------------------------------------


class TestGetCurrentUpgradingNode:
    def test_returns_none_when_no_upgrading_node(self):
        pool, _, _ = _make_pool(fetchone=None)
        svc = _make_service(pool)
        assert svc.get_current_upgrading_node() is None

    def test_returns_node_id_when_upgrading(self):
        pool, _, _ = _make_pool(fetchone=("node-xyz",))
        svc = _make_service(pool)
        assert svc.get_current_upgrading_node() == "node-xyz"

    def test_queries_upgrading_status(self):
        pool, _, cur = _make_pool(fetchone=None)
        svc = _make_service(pool)
        svc.get_current_upgrading_node()
        sql_called = cur.execute.call_args[0][0]
        assert "status = 'upgrading'" in sql_called


# ---------------------------------------------------------------------------
# _ensure_table (DDL bootstrap)
# ---------------------------------------------------------------------------


class TestEnsureTable:
    def test_creates_table_and_index_on_first_call(self):
        pool, _, cur = _make_pool()
        svc = UpgradeRegistryService(pool, "n1")
        assert svc._table_ensured is False
        svc._ensure_table()
        assert svc._table_ensured is True
        # Two DDL statements: CREATE TABLE and CREATE UNIQUE INDEX
        assert cur.execute.call_count == 2

    def test_skips_ddl_on_subsequent_calls(self):
        pool, _, cur = _make_pool()
        svc = UpgradeRegistryService(pool, "n1")
        svc._ensure_table()
        first_count = cur.execute.call_count
        svc._ensure_table()
        # No additional executes on second call
        assert cur.execute.call_count == first_count

    def test_table_ensured_flag_prevents_redundant_round_trips(self):
        pool, conn, _ = _make_pool()
        svc = UpgradeRegistryService(pool, "n1")
        svc._ensure_table()
        svc._ensure_table()
        # pool.connection() called only once (for the single DDL round-trip)
        assert pool.connection.call_count == 1
