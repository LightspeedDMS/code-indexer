"""Unit tests for TemporalMetadataPostgresBackend (Bug #1313 Step 6).

Root cause: TemporalMetadataStore (Story #669) was a SQLite-WAL database that,
in cluster mode, lives on the shared NFS golden-repos mount -- NFS cannot
satisfy SQLite WAL's -shm requirement, serializing all 8 indexing threads on
fsync. This backend replaces the storage ENGINE only (schema/operations are
identical) with PostgreSQL, eliminating the NFS bottleneck.

Mocked-pool tests (unconditional, mirror test_global_repos_postgres.py /
payload_cache_backend.py conventions) verify SQL text, parameterization, and
psycopg v3 API correctness (cursor-level executemany, not connection-level --
memory feedback_faithful_db_mocks).

Live-PG tests are gated by TEST_POSTGRES_DSN env var (skip when absent),
mirroring test_migration_runner.py / test_logs_postgres_backend.py.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

try:
    import psycopg  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


# ---------------------------------------------------------------------------
# Mocked-pool fixtures/helpers (mirror test_global_repos_postgres.py)
# ---------------------------------------------------------------------------


def _make_mock_pool(fetchone_return=None, fetchall_return=None, rowcount=1):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_return
    mock_cursor.fetchall.return_value = fetchall_return or []
    mock_cursor.rowcount = rowcount

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    # conn.execute (used for schema setup / single-row reads) delegates to a
    # fresh mock whose fetchone/fetchall mirror the cursor's, matching
    # payload_cache_backend.py's usage of conn.execute(...).fetchone().
    mock_conn.execute.return_value = mock_cursor

    mock_pool = MagicMock()

    @contextmanager
    def _connection():
        yield mock_conn

    mock_pool.connection.side_effect = _connection

    return mock_pool, mock_conn, mock_cursor


def _make_pool_if_available():
    """Return a ConnectionPool connected to a real test database, or None."""
    if not HAS_PSYCOPG:
        return None
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return None
    try:
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

        pool = ConnectionPool(dsn)
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return pool
    except Exception:
        return None


def _postgres_available() -> bool:
    """Cheap boolean availability check for ``@pytest.mark.skipif`` predicates.

    Bug #1313 round-2 rework (Codex non-blocking nit): unlike
    ``_make_pool_if_available()`` (which returns a live pool for tests to
    actually use), this helper opens a probe pool purely to verify
    connectivity and closes it immediately before returning, so pytest
    collection doesn't leak a ``ConnectionPool`` that
    ``psycopg_pool.ConnectionPool.__del__`` warns about via
    ``PytestUnraisableExceptionWarning``.
    """
    if not HAS_PSYCOPG:
        return False
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return False
    try:
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

        pool = ConnectionPool(dsn)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        finally:
            pool.close()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestTemporalMetadataPostgresBackendProtocolCompliance:
    def test_isinstance_check_passes(self):
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="abc123")

        assert isinstance(backend, TemporalMetadataBackend)


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------


class TestEnsureSchema:
    def test_constructor_creates_table_idempotently(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool()
        TemporalMetadataPostgresBackend(mock_pool, collection_key="abc123")

        executed_sql = " ".join(
            call.args[0] for call in mock_conn.execute.call_args_list
        )
        assert "CREATE TABLE IF NOT EXISTS temporal_metadata" in executed_sql

    def test_constructor_creates_indexes_matching_migration_033(self):
        """Bug #1313 round-2 rework (Codex Finding B): the defensive
        _ensure_schema DDL must be byte-consistent with migration
        033_temporal_metadata.sql, which creates a UNIQUE index on
        (collection_key, point_id) and a plain index on
        (collection_key, commit_hash) in addition to the table itself."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool()
        TemporalMetadataPostgresBackend(mock_pool, collection_key="abc123")

        executed_sql = " ".join(
            call.args[0] for call in mock_conn.execute.call_args_list
        )
        assert (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_temporal_meta_pointid"
            in executed_sql
        )
        assert "collection_key, point_id" in executed_sql
        assert "CREATE INDEX IF NOT EXISTS idx_temporal_meta_commit" in executed_sql
        assert "collection_key, commit_hash" in executed_sql


# ---------------------------------------------------------------------------
# Schema setup failures must fail loud (Bug #1313 round-2 rework, Finding B)
# ---------------------------------------------------------------------------


class TestEnsureSchemaFailsLoud:
    def test_schema_setup_failure_raises_not_swallowed(self):
        """Prior to this fix, _ensure_schema wrapped the DDL in a bare
        except Exception that logged a warning and returned a constructed
        backend anyway -- so a broken/missing migration, or a permissions
        error, was silently deferred to the first temporal write/read
        instead of failing at storage initialization. Construction must now
        raise."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool()
        mock_conn.execute.side_effect = RuntimeError("boom: DDL failed")

        with pytest.raises(RuntimeError, match="boom: DDL failed"):
            TemporalMetadataPostgresBackend(mock_pool, collection_key="abc123")


# ---------------------------------------------------------------------------
# save_metadata_batch
# ---------------------------------------------------------------------------


class TestSaveMetadataBatch:
    def test_empty_rows_returns_empty_list_without_touching_pool(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.reset_mock()
        mock_cursor.reset_mock()

        result = backend.save_metadata_batch([])

        assert result == []
        mock_cursor.executemany.assert_not_called()

    def test_executemany_used_on_cursor_not_connection(self):
        """Bug feedback_faithful_db_mocks: psycopg v3 executemany lives on the
        CURSOR, not the connection -- verify the backend uses it correctly."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        point_id = "project:diff:abc:file.py:0"
        payload = {"commit_hash": "abc", "path": "file.py", "chunk_index": 0}

        backend.save_metadata_batch([(point_id, payload)])

        mock_cursor.executemany.assert_called_once()
        mock_conn.executemany.assert_not_called()

    def test_returns_hash_prefixes_in_input_order_via_shared_helper(self):
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        rows = [
            ("project:diff:a:file1.py:0", {"commit_hash": "a", "path": "file1.py"}),
            ("project:diff:b:file2.py:0", {"commit_hash": "b", "path": "file2.py"}),
        ]

        result = backend.save_metadata_batch(rows)

        assert result == [
            generate_hash_prefix("project:diff:a:file1.py:0"),
            generate_hash_prefix("project:diff:b:file2.py:0"),
        ]

    def test_uses_on_conflict_upsert_sql(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        backend.save_metadata_batch(
            [("pid", {"commit_hash": "c", "path": "f.py", "chunk_index": 0})]
        )

        sql = mock_cursor.executemany.call_args[0][0]
        assert "INSERT INTO temporal_metadata" in sql
        assert "ON CONFLICT" in sql
        assert "collection_key" in sql
        assert "%s" in sql

    def test_sets_local_synchronous_commit_off(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.execute.reset_mock()

        backend.save_metadata_batch(
            [("pid", {"commit_hash": "c", "path": "f.py", "chunk_index": 0})]
        )

        executed_sql = " ".join(
            call.args[0] for call in mock_conn.execute.call_args_list
        )
        assert "SET LOCAL synchronous_commit = off" in executed_sql

    def test_commits_once(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.commit.reset_mock()

        backend.save_metadata_batch(
            [("pid", {"commit_hash": "c", "path": "f.py", "chunk_index": 0})]
        )

        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# save_metadata (single-row convenience wrapper)
# ---------------------------------------------------------------------------


class TestSaveMetadata:
    def test_delegates_to_save_metadata_batch_and_returns_hash_prefix(self):
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        point_id = "project:diff:single:file.py:0"
        result = backend.save_metadata(
            point_id, {"commit_hash": "x", "path": "file.py", "chunk_index": 0}
        )

        assert result == generate_hash_prefix(point_id)


# ---------------------------------------------------------------------------
# checkpoint_wal
# ---------------------------------------------------------------------------


class TestCheckpointWal:
    def test_is_a_no_op(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.reset_mock()
        mock_cursor.reset_mock()

        backend.checkpoint_wal()  # must not raise

        mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# get_point_id / get_metadata
# ---------------------------------------------------------------------------


class TestGetPointId:
    def test_returns_point_id_scoped_by_collection_key(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool(fetchone_return=("my-point-id",))
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.execute.reset_mock()

        result = backend.get_point_id("hashprefix1234ab")

        assert result == "my-point-id"
        sql = mock_conn.execute.call_args[0][0]
        params = mock_conn.execute.call_args[0][1]
        assert "WHERE collection_key = %s" in sql
        assert "hash_prefix = %s" in sql
        assert params == ("key1", "hashprefix1234ab")

    def test_returns_none_when_not_found(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchone_return=None)
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        assert backend.get_point_id("missing") is None


class TestGetMetadata:
    def test_returns_full_metadata_dict(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        row = ("pid-1", "commit-1", "path/to.py", 3, "2024-01-01T00:00:00")
        mock_pool, _, _ = _make_mock_pool(fetchone_return=row)
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        result = backend.get_metadata("hashprefix1234ab")

        assert result == {
            "point_id": "pid-1",
            "commit_hash": "commit-1",
            "file_path": "path/to.py",
            "chunk_index": 3,
            "created_at": "2024-01-01T00:00:00",
        }

    def test_returns_none_when_not_found(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchone_return=None)
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        assert backend.get_metadata("missing") is None


# ---------------------------------------------------------------------------
# delete_metadata
# ---------------------------------------------------------------------------


class TestDeleteMetadata:
    def test_executes_delete_scoped_by_collection_key(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool()
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.execute.reset_mock()
        mock_conn.commit.reset_mock()

        backend.delete_metadata("hashprefix1234ab")

        sql = mock_conn.execute.call_args[0][0]
        params = mock_conn.execute.call_args[0][1]
        assert "DELETE FROM temporal_metadata" in sql
        assert "collection_key = %s" in sql
        assert params == ("key1", "hashprefix1234ab")
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# cleanup_stale_metadata
# ---------------------------------------------------------------------------


class TestCleanupStaleMetadata:
    def test_removes_entries_not_in_valid_set_scoped_by_collection_key(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool(
            fetchall_return=[("hash1",), ("hash2",), ("hash3",)]
        )
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        removed = backend.cleanup_stale_metadata({"hash1", "hash3"})

        assert removed == 1

    def test_returns_zero_when_nothing_stale(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchall_return=[("hash1",)])
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")

        removed = backend.cleanup_stale_metadata({"hash1"})

        assert removed == 0


# ---------------------------------------------------------------------------
# count_entries
# ---------------------------------------------------------------------------


class TestCountEntries:
    def test_returns_count_scoped_by_collection_key(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool(fetchone_return=(7,))
        backend = TemporalMetadataPostgresBackend(mock_pool, collection_key="key1")
        mock_conn.execute.reset_mock()

        result = backend.count_entries()

        assert result == 7
        sql = mock_conn.execute.call_args[0][0]
        params = mock_conn.execute.call_args[0][1]
        assert "COUNT(*)" in sql
        assert "collection_key = %s" in sql
        assert params == ("key1",)


# ---------------------------------------------------------------------------
# collection_key scoping (isolation between two collections)
# ---------------------------------------------------------------------------


class TestCollectionKeyScoping:
    def test_different_collection_keys_use_different_scoping_params(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool(fetchone_return=None)
        backend_a = TemporalMetadataPostgresBackend(mock_pool, collection_key="key-A")
        backend_b = TemporalMetadataPostgresBackend(mock_pool, collection_key="key-B")

        backend_a.get_point_id("shared-hash-prefix")
        params_a = mock_conn.execute.call_args[0][1]

        backend_b.get_point_id("shared-hash-prefix")
        params_b = mock_conn.execute.call_args[0][1]

        assert params_a == ("key-A", "shared-hash-prefix")
        assert params_b == ("key-B", "shared-hash-prefix")


# ---------------------------------------------------------------------------
# make_postgres_temporal_metadata_factory (Bug #1313 round-3): the SINGLE
# shared definition of the PG factory shape, used by BOTH lifespan.py
# (in-process server wiring) and temporal_child_wiring.py (child-process
# bootstrap wiring) so both compute an IDENTICAL collection_key for the same
# collection_path.
# ---------------------------------------------------------------------------


class TestMakePostgresTemporalMetadataFactory:
    def test_factory_product_is_temporal_metadata_postgres_backend(self):
        from pathlib import Path

        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
            make_postgres_temporal_metadata_factory,
        )

        mock_pool, _, _ = _make_mock_pool()
        factory = make_postgres_temporal_metadata_factory(mock_pool)

        backend = factory(Path("/some/collection/path"))

        assert isinstance(backend, TemporalMetadataPostgresBackend)

    def test_collection_key_matches_sha256_prefix_formula(self):
        import hashlib
        from pathlib import Path

        from code_indexer.storage.temporal_metadata_store import (
            COLLECTION_KEY_LENGTH,
        )
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            make_postgres_temporal_metadata_factory,
        )

        mock_pool, _, _ = _make_mock_pool()
        factory = make_postgres_temporal_metadata_factory(mock_pool)

        collection_path = Path("/some/collection/path")
        backend = factory(collection_path)

        expected_key = hashlib.sha256(str(collection_path).encode()).hexdigest()[
            :COLLECTION_KEY_LENGTH
        ]
        assert backend._collection_key == expected_key

    def test_bound_to_the_given_pool(self):
        from pathlib import Path

        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            make_postgres_temporal_metadata_factory,
        )

        mock_pool, _, _ = _make_mock_pool()
        factory = make_postgres_temporal_metadata_factory(mock_pool)

        backend = factory(Path("/a/b"))

        assert backend._pool is mock_pool

    def test_two_different_paths_produce_different_collection_keys(self):
        from pathlib import Path

        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            make_postgres_temporal_metadata_factory,
        )

        mock_pool, _, _ = _make_mock_pool()
        factory = make_postgres_temporal_metadata_factory(mock_pool)

        backend_a = factory(Path("/collection/a"))
        backend_b = factory(Path("/collection/b"))

        assert backend_a._collection_key != backend_b._collection_key


# ---------------------------------------------------------------------------
# Finding 1 (Bug #1313 review): module must not transitively require
# psycopg_pool merely to be IMPORTED -- ConnectionPool is only used as a type
# annotation on the constructor parameter, never instantiated by this module.
# ---------------------------------------------------------------------------


class TestImportsWithoutPsycopgPool:
    def test_module_imports_successfully_without_psycopg_pool_installed(self):
        """Simulate psycopg_pool being absent (e.g. not installed in a slim
        deployment) via sys.meta_path blocking, and confirm that importing
        temporal_metadata_backend still succeeds. Prior to the Finding 1 fix,
        this module did `from .connection_pool import ConnectionPool`, which
        transitively triggers connection_pool.py's module-level
        `from psycopg_pool import ConnectionPool as _PsycopgPool` -- so merely
        importing this module required psycopg_pool to be installed, even
        though the type is only used for a constructor parameter annotation.
        """
        import importlib
        import sys

        module_names_to_evict = [
            "code_indexer.server.storage.postgres.temporal_metadata_backend",
            "code_indexer.server.storage.postgres.connection_pool",
        ] + [
            name
            for name in list(sys.modules)
            if name == "psycopg_pool" or name.startswith("psycopg_pool.")
        ]
        saved_modules = {
            name: sys.modules.pop(name)
            for name in module_names_to_evict
            if name in sys.modules
        }

        class _PsycopgPoolBlocker:
            """Meta-path finder that makes psycopg_pool appear uninstalled.

            Implements ``find_spec`` (the modern meta-path-finder API, per
            importlib.abc.MetaPathFinder / typeshed's MetaPathFinderProtocol)
            rather than the deprecated find_module/load_module pair, so this
            satisfies both mypy's ``sys.meta_path: List[MetaPathFinderProtocol]``
            typing and real import-machinery dispatch.
            """

            def find_spec(self, name, path=None, target=None):
                if name == "psycopg_pool" or name.startswith("psycopg_pool."):
                    raise ModuleNotFoundError(
                        f"No module named {name!r} (blocked for test)"
                    )
                return None

        blocker = _PsycopgPoolBlocker()
        sys.meta_path.insert(0, blocker)
        try:
            imported = importlib.import_module(
                "code_indexer.server.storage.postgres.temporal_metadata_backend"
            )
            assert imported is not None
        finally:
            sys.meta_path.remove(blocker)
            for name in module_names_to_evict:
                sys.modules.pop(name, None)
            for name, mod in saved_modules.items():
                sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Live-PG tests (gated by TEST_POSTGRES_DSN; skip when absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _postgres_available(),
    reason="TEST_POSTGRES_DSN not set or PostgreSQL unavailable",
)
class TestTemporalMetadataPostgresBackendLivePg:
    """Real PostgreSQL round-trip tests. Skipped unless TEST_POSTGRES_DSN is set."""

    def test_save_and_get_round_trip_against_real_postgres(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_pool_if_available()
        assert pool is not None
        backend = TemporalMetadataPostgresBackend(
            pool, collection_key="live-test-key-1313"
        )

        try:
            point_id = "project:diff:livepg:file.py:0"
            payload = {"commit_hash": "livepg", "path": "file.py", "chunk_index": 0}

            hash_prefixes = backend.save_metadata_batch([(point_id, payload)])

            assert backend.get_point_id(hash_prefixes[0]) == point_id
            metadata = backend.get_metadata(hash_prefixes[0])
            assert metadata is not None
            assert metadata["commit_hash"] == "livepg"

            # Upsert: re-index same point_id overwrites, no duplicate
            backend.save_metadata_batch(
                [
                    (
                        point_id,
                        {
                            "commit_hash": "livepg-v2",
                            "path": "file.py",
                            "chunk_index": 0,
                        },
                    )
                ]
            )
            assert backend.get_metadata(hash_prefixes[0])["commit_hash"] == "livepg-v2"
        finally:
            with pool.connection() as conn:
                conn.execute(
                    "DELETE FROM temporal_metadata WHERE collection_key = %s",
                    ("live-test-key-1313",),
                )
                conn.commit()

    def test_batch_insert_multiple_rows_lands_in_order_against_real_postgres(self):
        """Finding 2: prove save_metadata_batch's multi-row insert actually
        persists all rows, in order, with correct hash prefixes, against a
        REAL PostgreSQL instance (not a MagicMock)."""
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_pool_if_available()
        assert pool is not None
        collection_key = f"live-test-batch-{uuid.uuid4().hex}"
        backend = TemporalMetadataPostgresBackend(pool, collection_key=collection_key)

        try:
            rows = [
                (
                    f"project:diff:batch:file{i}.py:0",
                    {"commit_hash": "batchc", "path": f"file{i}.py", "chunk_index": i},
                )
                for i in range(5)
            ]

            hash_prefixes = backend.save_metadata_batch(rows)

            assert hash_prefixes == [generate_hash_prefix(pid) for pid, _ in rows]
            assert backend.count_entries() == 5
            for (point_id, payload), hash_prefix in zip(rows, hash_prefixes):
                assert backend.get_point_id(hash_prefix) == point_id
                metadata = backend.get_metadata(hash_prefix)
                assert metadata is not None
                assert metadata["file_path"] == payload["path"]
                assert metadata["chunk_index"] == payload["chunk_index"]
        finally:
            with pool.connection() as conn:
                conn.execute(
                    "DELETE FROM temporal_metadata WHERE collection_key = %s",
                    (collection_key,),
                )
                conn.commit()

    def test_reupsert_same_point_id_no_unique_violation_and_no_duplicate_row_1313_finding4(
        self,
    ):
        """Finding 4: writing the SAME point_id twice (same collection_key)
        must never raise a UNIQUE(collection_key, point_id) violation and must
        never create a duplicate row -- ON CONFLICT (collection_key,
        hash_prefix) coincides with true replace-by-point_id semantics since
        hash_prefix is deterministically derived from point_id. Proven here
        against REAL PostgreSQL (the actual unique index is only enforced by
        the real database, never by a mock)."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_pool_if_available()
        assert pool is not None
        collection_key = f"live-test-reupsert-{uuid.uuid4().hex}"
        backend = TemporalMetadataPostgresBackend(pool, collection_key=collection_key)

        try:
            point_id = "project:diff:reupsert:file.py:0"

            hash_prefixes_1 = backend.save_metadata_batch(
                [(point_id, {"commit_hash": "v1", "path": "file.py", "chunk_index": 0})]
            )
            assert backend.count_entries() == 1

            # Re-upsert same point_id -- must not raise, must not duplicate.
            hash_prefixes_2 = backend.save_metadata_batch(
                [(point_id, {"commit_hash": "v2", "path": "file.py", "chunk_index": 0})]
            )

            assert hash_prefixes_1 == hash_prefixes_2
            assert backend.count_entries() == 1
            assert backend.get_metadata(hash_prefixes_2[0])["commit_hash"] == "v2"
        finally:
            with pool.connection() as conn:
                conn.execute(
                    "DELETE FROM temporal_metadata WHERE collection_key = %s",
                    (collection_key,),
                )
                conn.commit()

    def test_cleanup_stale_metadata_scoped_by_collection_key_against_real_postgres(
        self,
    ):
        """Finding 2: cleanup_stale_metadata must remove only stale rows under
        the TARGETED collection_key, leaving rows under a DIFFERENT
        collection_key completely untouched."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_pool_if_available()
        assert pool is not None
        key_a = f"live-test-cleanup-a-{uuid.uuid4().hex}"
        key_b = f"live-test-cleanup-b-{uuid.uuid4().hex}"
        backend_a = TemporalMetadataPostgresBackend(pool, collection_key=key_a)
        backend_b = TemporalMetadataPostgresBackend(pool, collection_key=key_b)

        try:
            rows_a = [
                (
                    f"project:diff:cleanup:a{i}.py:0",
                    {"commit_hash": "a", "path": f"a{i}.py"},
                )
                for i in range(3)
            ]
            rows_b = [
                (
                    f"project:diff:cleanup:b{i}.py:0",
                    {"commit_hash": "b", "path": f"b{i}.py"},
                )
                for i in range(2)
            ]
            hash_prefixes_a = backend_a.save_metadata_batch(rows_a)
            hash_prefixes_b = backend_b.save_metadata_batch(rows_b)

            # Keep only the first hash prefix under key_a as "valid".
            removed = backend_a.cleanup_stale_metadata({hash_prefixes_a[0]})

            assert removed == 2
            assert backend_a.count_entries() == 1
            assert backend_a.get_point_id(hash_prefixes_a[0]) is not None
            assert backend_a.get_point_id(hash_prefixes_a[1]) is None
            assert backend_a.get_point_id(hash_prefixes_a[2]) is None

            # key_b rows are completely untouched.
            assert backend_b.count_entries() == 2
            for hash_prefix in hash_prefixes_b:
                assert backend_b.get_point_id(hash_prefix) is not None
        finally:
            with pool.connection() as conn:
                conn.execute(
                    "DELETE FROM temporal_metadata WHERE collection_key IN (%s, %s)",
                    (key_a, key_b),
                )
                conn.commit()

    def test_collection_key_isolation_same_hash_prefix_different_keys_against_real_postgres(
        self,
    ):
        """Finding 2: the same hash_prefix (same point_id) written under TWO
        different collection_keys must not collide -- rows must be visible
        ONLY through the collection_key they were written under."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_pool_if_available()
        assert pool is not None
        key_a = f"live-test-isolation-a-{uuid.uuid4().hex}"
        key_b = f"live-test-isolation-b-{uuid.uuid4().hex}"
        backend_a = TemporalMetadataPostgresBackend(pool, collection_key=key_a)
        backend_b = TemporalMetadataPostgresBackend(pool, collection_key=key_b)

        try:
            point_id = "project:diff:isolation:shared.py:0"

            hash_prefixes_a = backend_a.save_metadata_batch(
                [(point_id, {"commit_hash": "under-a", "path": "shared.py"})]
            )

            # Not written under key_b at all -- must be invisible there.
            assert backend_b.get_point_id(hash_prefixes_a[0]) is None
            assert backend_b.get_metadata(hash_prefixes_a[0]) is None

            # Still visible under key_a.
            assert backend_a.get_point_id(hash_prefixes_a[0]) == point_id
            assert (
                backend_a.get_metadata(hash_prefixes_a[0])["commit_hash"] == "under-a"
            )
        finally:
            with pool.connection() as conn:
                conn.execute(
                    "DELETE FROM temporal_metadata WHERE collection_key IN (%s, %s)",
                    (key_a, key_b),
                )
                conn.commit()

    def test_count_entries_reflects_real_row_count_against_real_postgres(self):
        """Finding 2: count_entries() must reflect the actual persisted row
        count scoped to collection_key, verified against a real table."""
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        pool = _make_pool_if_available()
        assert pool is not None
        collection_key = f"live-test-count-{uuid.uuid4().hex}"
        backend = TemporalMetadataPostgresBackend(pool, collection_key=collection_key)

        try:
            assert backend.count_entries() == 0

            rows = [
                (
                    f"project:diff:count:file{i}.py:0",
                    {"commit_hash": "c", "path": f"file{i}.py"},
                )
                for i in range(4)
            ]
            backend.save_metadata_batch(rows)

            assert backend.count_entries() == 4

            with pool.connection() as conn:
                raw_count = conn.execute(
                    "SELECT COUNT(*) FROM temporal_metadata WHERE collection_key = %s",
                    (collection_key,),
                ).fetchone()[0]
            assert raw_count == 4
        finally:
            with pool.connection() as conn:
                conn.execute(
                    "DELETE FROM temporal_metadata WHERE collection_key = %s",
                    (collection_key,),
                )
                conn.commit()
