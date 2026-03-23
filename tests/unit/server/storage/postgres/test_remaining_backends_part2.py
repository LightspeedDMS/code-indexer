"""
Unit tests for PostgreSQL backends: DependencyMapTracking, GitCredentials, RepoCategory (Story #414).

Verifies that each backend:
  1. Satisfies its Protocol (isinstance check).
  2. Exposes all required method names.
  3. Uses %s (not ?) placeholders — correct for psycopg v3.

All tests use a MagicMock connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool() -> MagicMock:
    """Return a MagicMock mimicking a psycopg ConnectionPool context-manager."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    cursor.rowcount = 1
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> MagicMock:
    """Return the mock connection from the pool context manager."""
    return pool.connection.return_value.__enter__.return_value


# ---------------------------------------------------------------------------
# DependencyMapTrackingPostgresBackend
# ---------------------------------------------------------------------------


class TestDependencyMapTrackingPostgresBackend:
    def test_satisfies_protocol(self) -> None:
        """DependencyMapTrackingPostgresBackend must satisfy the Protocol."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )
        from code_indexer.server.storage.protocols import DependencyMapTrackingBackend

        backend = DependencyMapTrackingPostgresBackend(_make_pool())
        assert isinstance(backend, DependencyMapTrackingBackend)

    def test_required_methods_present(self) -> None:
        """All protocol methods must be present."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        required = {
            "get_tracking",
            "update_tracking",
            "cleanup_stale_status_on_startup",
            "record_run_metrics",
            "get_run_history",
            "close",
        }
        for method in required:
            assert hasattr(
                DependencyMapTrackingPostgresBackend, method
            ), f"Missing method: {method}"

    def test_update_tracking_uses_percent_s(self) -> None:
        """update_tracking must use %s placeholders."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        pool = _make_pool()
        backend = DependencyMapTrackingPostgresBackend(pool)
        backend.update_tracking(status="running")

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_update_tracking_noop_when_no_fields(self) -> None:
        """update_tracking with no arguments must not execute any SQL."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        pool = _make_pool()
        backend = DependencyMapTrackingPostgresBackend(pool)
        backend.update_tracking()

        conn = _get_conn(pool)
        conn.execute.assert_not_called()

    def test_get_tracking_initialises_singleton_when_absent(self) -> None:
        """get_tracking must INSERT singleton row when it does not exist."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        pool = _make_pool()
        conn = _get_conn(pool)
        inserted_row = (1, None, None, "pending", None, None, None, None)
        conn.execute.return_value.fetchone.side_effect = [None, inserted_row]

        backend = DependencyMapTrackingPostgresBackend(pool)
        result = backend.get_tracking()

        assert result["status"] == "pending"
        calls = conn.execute.call_args_list
        insert_calls = [c for c in calls if "INSERT" in str(c[0][0]).upper()]
        assert len(insert_calls) >= 1

    def test_cleanup_stale_status_returns_bool(self) -> None:
        """cleanup_stale_status_on_startup must return a bool."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        pool = _make_pool()
        result = DependencyMapTrackingPostgresBackend(
            pool
        ).cleanup_stale_status_on_startup()
        assert isinstance(result, bool)

    def test_record_run_metrics_uses_percent_s(self) -> None:
        """record_run_metrics must use %s placeholders."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        pool = _make_pool()
        backend = DependencyMapTrackingPostgresBackend(pool)
        backend.record_run_metrics({"timestamp": "2026-01-01", "domain_count": 5})

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_get_run_history_returns_list(self) -> None:
        """get_run_history must return a list."""
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )

        pool = _make_pool()
        result = DependencyMapTrackingPostgresBackend(pool).get_run_history()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# GitCredentialsPostgresBackend
# ---------------------------------------------------------------------------


class TestGitCredentialsPostgresBackend:
    def test_satisfies_protocol(self) -> None:
        """GitCredentialsPostgresBackend must satisfy the GitCredentialsBackend Protocol."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )
        from code_indexer.server.storage.protocols import GitCredentialsBackend

        backend = GitCredentialsPostgresBackend(_make_pool())
        assert isinstance(backend, GitCredentialsBackend)

    def test_required_methods_present(self) -> None:
        """All protocol methods must be present."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        required = {
            "upsert_credential",
            "list_credentials",
            "delete_credential",
            "get_credential_for_host",
            "close",
        }
        for method in required:
            assert hasattr(
                GitCredentialsPostgresBackend, method
            ), f"Missing method: {method}"

    def test_upsert_credential_uses_percent_s(self) -> None:
        """upsert_credential must use %s placeholders."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        pool = _make_pool()
        backend = GitCredentialsPostgresBackend(pool)
        backend.upsert_credential(
            credential_id="cred-1",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="enc_tok",
        )

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_upsert_credential_uses_on_conflict(self) -> None:
        """upsert_credential must use ON CONFLICT for upsert semantics."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        pool = _make_pool()
        backend = GitCredentialsPostgresBackend(pool)
        backend.upsert_credential(
            credential_id="cred-1",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="enc_tok",
        )

        conn = _get_conn(pool)
        sql = conn.execute.call_args[0][0].upper()
        assert "ON CONFLICT" in sql

    def test_list_credentials_returns_list(self) -> None:
        """list_credentials must return a list."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        pool = _make_pool()
        result = GitCredentialsPostgresBackend(pool).list_credentials("alice")
        assert isinstance(result, list)

    def test_delete_credential_returns_bool(self) -> None:
        """delete_credential must return a bool."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        pool = _make_pool()
        result = GitCredentialsPostgresBackend(pool).delete_credential(
            "alice", "cred-1"
        )
        assert isinstance(result, bool)

    def test_get_credential_for_host_returns_none_when_absent(self) -> None:
        """get_credential_for_host returns None when no matching row."""
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )

        pool = _make_pool()
        result = GitCredentialsPostgresBackend(pool).get_credential_for_host(
            "alice", "github.com"
        )
        assert result is None


# ---------------------------------------------------------------------------
# RepoCategoryPostgresBackend
# ---------------------------------------------------------------------------


class TestRepoCategoryPostgresBackend:
    def test_satisfies_protocol(self) -> None:
        """RepoCategoryPostgresBackend must satisfy the RepoCategoryBackend Protocol."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )
        from code_indexer.server.storage.protocols import RepoCategoryBackend

        backend = RepoCategoryPostgresBackend(_make_pool())
        assert isinstance(backend, RepoCategoryBackend)

    def test_required_methods_present(self) -> None:
        """All protocol methods must be present."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        required = {
            "create_category",
            "list_categories",
            "get_category",
            "update_category",
            "delete_category",
            "reorder_categories",
            "shift_all_priorities",
            "get_next_priority",
            "get_repo_category_map",
            "close",
        }
        for method in required:
            assert hasattr(
                RepoCategoryPostgresBackend, method
            ), f"Missing method: {method}"

    def test_create_category_uses_returning(self) -> None:
        """create_category must use RETURNING id (PostgreSQL idiom)."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        conn = _get_conn(pool)
        conn.execute.return_value.fetchone.return_value = (7,)

        category_id = RepoCategoryPostgresBackend(pool).create_category(
            "backend", ".*backend.*", 1
        )

        assert category_id == 7
        sql = conn.execute.call_args[0][0].upper()
        assert "RETURNING" in sql

    def test_create_category_uses_percent_s(self) -> None:
        """create_category must use %s placeholders."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        conn = _get_conn(pool)
        conn.execute.return_value.fetchone.return_value = (1,)

        RepoCategoryPostgresBackend(pool).create_category("backend", ".*backend.*", 1)

        sql = conn.execute.call_args[0][0]
        assert "%s" in sql
        assert "?" not in sql

    def test_list_categories_returns_list(self) -> None:
        """list_categories must return a list."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        result = RepoCategoryPostgresBackend(pool).list_categories()
        assert isinstance(result, list)

    def test_get_category_returns_none_when_absent(self) -> None:
        """get_category returns None when the category does not exist."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        result = RepoCategoryPostgresBackend(pool).get_category(999)
        assert result is None

    def test_get_next_priority_returns_int(self) -> None:
        """get_next_priority must return an integer."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        result = RepoCategoryPostgresBackend(pool).get_next_priority()
        assert isinstance(result, int)

    def test_reorder_categories_uses_percent_s(self) -> None:
        """reorder_categories must use %s placeholders."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        RepoCategoryPostgresBackend(pool).reorder_categories([3, 1, 2])

        conn = _get_conn(pool)
        assert conn.execute.call_count >= 1
        last_sql = conn.execute.call_args_list[-1][0][0]
        assert "%s" in last_sql
        assert "?" not in last_sql

    def test_get_repo_category_map_returns_dict(self) -> None:
        """get_repo_category_map must return a dict."""
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )

        pool = _make_pool()
        result = RepoCategoryPostgresBackend(pool).get_repo_category_map()
        assert isinstance(result, dict)
