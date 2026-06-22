"""Tests for store_batch on PayloadCachePostgresBackend (Bug #1181).

Verifies:
- store_batch method exists
- store_batch issues SET LOCAL synchronous_commit = off
- store_batch calls conn.commit() exactly ONCE for N entries
- store() also issues SET LOCAL synchronous_commit = off
- store_batch([]) is a no-op (no connection acquired)

No live PostgreSQL required — uses a mock pool.

TDD: tests written BEFORE implementation.
"""

from typing import List
from unittest.mock import MagicMock


def _make_backend_with_tracking():
    """Build a PayloadCachePostgresBackend with a fake pool that records SQL statements."""
    from code_indexer.server.storage.postgres.payload_cache_backend import (
        PayloadCachePostgresBackend,
    )

    executed_statements: List[str] = []
    commit_count: List[int] = [0]
    connection_acquisitions: List[int] = [0]

    class FakeConn:
        def execute(self, sql, params=None):
            executed_statements.append(sql.strip())
            return MagicMock(rowcount=len(params) if isinstance(params, list) else 1)

        def executemany(self, sql, params_list):
            executed_statements.append(sql.strip())

        def commit(self):
            commit_count[0] += 1

        def __enter__(self):
            connection_acquisitions[0] += 1
            return self

        def __exit__(self, *a):
            pass

    class FakePool:
        def connection(self):
            return FakeConn()

    backend = PayloadCachePostgresBackend.__new__(PayloadCachePostgresBackend)
    backend._pool = FakePool()

    return backend, executed_statements, commit_count, connection_acquisitions


class TestPayloadCachePostgresBackendStoreBatch:
    """Tests for store_batch on PayloadCachePostgresBackend (Bug #1181)."""

    def test_postgres_backend_has_store_batch_method(self):
        """PayloadCachePostgresBackend must have a store_batch method."""
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        assert hasattr(PayloadCachePostgresBackend, "store_batch"), (
            "PayloadCachePostgresBackend must have store_batch() method"
        )

    def test_store_batch_issues_synchronous_commit_off(self):
        """store_batch must issue SET LOCAL synchronous_commit = off before INSERT."""
        backend, executed_statements, commit_count, _ = _make_backend_with_tracking()

        entries = [
            ("handle-1", "content-1", "prev-1", 300),
            ("handle-2", "content-2", "prev-2", 300),
        ]
        backend.store_batch(entries)

        sync_commit_stmts = [
            s for s in executed_statements if "synchronous_commit" in s.lower()
        ]
        assert len(sync_commit_stmts) >= 1, (
            f"Expected SET LOCAL synchronous_commit in store_batch statements, "
            f"got: {executed_statements}"
        )
        assert "off" in sync_commit_stmts[0].lower(), (
            f"synchronous_commit must be set to 'off', got: {sync_commit_stmts[0]}"
        )

    def test_store_batch_single_commit_for_n_entries(self):
        """store_batch must call conn.commit() exactly ONCE for N entries."""
        backend, _, commit_count, _ = _make_backend_with_tracking()

        entries = [
            ("h1", "c1", "p1", 300),
            ("h2", "c2", "p2", 300),
            ("h3", "c3", "p3", 300),
        ]
        backend.store_batch(entries)

        assert commit_count[0] == 1, (
            f"Expected exactly 1 commit for store_batch of 3 entries, got {commit_count[0]}"
        )

    def test_store_batch_empty_is_noop_no_connection(self):
        """store_batch([]) must return without acquiring a connection."""
        backend, _, _, connection_acquisitions = _make_backend_with_tracking()

        backend.store_batch([])

        assert connection_acquisitions[0] == 0, (
            f"store_batch([]) must not acquire a connection, got {connection_acquisitions[0]}"
        )

    def test_store_also_issues_synchronous_commit_off(self):
        """store() must also issue SET LOCAL synchronous_commit = off before INSERT."""
        backend, executed_statements, _, _ = _make_backend_with_tracking()

        backend.store("handle-x", "content-x", "prev-x", 300)

        sync_commit_stmts = [
            s for s in executed_statements if "synchronous_commit" in s.lower()
        ]
        assert len(sync_commit_stmts) >= 1, (
            f"Expected SET LOCAL synchronous_commit = off in store(), "
            f"got: {executed_statements}"
        )
        assert "off" in sync_commit_stmts[0].lower()

    def test_store_batch_synchronous_commit_issued_before_insert(self):
        """SET LOCAL synchronous_commit = off must appear BEFORE the INSERT in store_batch."""
        backend, executed_statements, _, _ = _make_backend_with_tracking()

        entries = [("h1", "c1", "p1", 300)]
        backend.store_batch(entries)

        sync_idx = next(
            (
                i
                for i, s in enumerate(executed_statements)
                if "synchronous_commit" in s.lower()
            ),
            None,
        )
        insert_idx = next(
            (i for i, s in enumerate(executed_statements) if "INSERT" in s.upper()),
            None,
        )
        assert sync_idx is not None, "synchronous_commit statement not found"
        assert insert_idx is not None, "INSERT statement not found"
        assert sync_idx < insert_idx, (
            f"SET LOCAL synchronous_commit = off (idx {sync_idx}) must precede "
            f"INSERT (idx {insert_idx}). Statements: {executed_statements}"
        )
