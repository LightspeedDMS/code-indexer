"""
Unit tests for GlobalReposPostgresBackend.

Story #412: PostgreSQL Backend for GlobalRepos and GoldenRepoMetadata

All tests use a mocked ConnectionPool — no real PostgreSQL required.
Verifies:
  - Protocol compliance (isinstance check against GlobalReposBackend)
  - All method signatures match the Protocol
  - SQL generation and parameterization are correct
  - Return values match expected semantics (True/False, dict/None, etc.)
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_mock_pool(fetchone_return=None, fetchall_return=None, rowcount=1):
    """
    Build a mock ConnectionPool whose connection() context manager yields
    a mock psycopg connection with a cursor whose fetchone/fetchall are
    pre-configured.
    """
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_return
    mock_cursor.fetchall.return_value = fetchall_return or []
    mock_cursor.rowcount = rowcount

    mock_conn = MagicMock()
    # cursor() must work as a context manager
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()

    @contextmanager
    def _connection():
        yield mock_conn

    mock_pool.connection.side_effect = _connection

    return mock_pool, mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestGlobalReposProtocolCompliance:
    """Verify GlobalReposPostgresBackend satisfies the GlobalReposBackend Protocol."""

    def test_isinstance_check_passes(self) -> None:
        """
        Given GlobalReposPostgresBackend is instantiated with a mock pool
        When isinstance(backend, GlobalReposBackend) is called
        Then it returns True (structural subtyping via @runtime_checkable).
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )
        from code_indexer.server.storage.protocols import GlobalReposBackend

        mock_pool, _, _ = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        assert isinstance(backend, GlobalReposBackend)

    def test_has_all_required_methods(self) -> None:
        """
        Given GlobalReposPostgresBackend is instantiated
        When the Protocol's required methods are checked
        Then all are present as callables.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        required = [
            "register_repo",
            "get_repo",
            "list_repos",
            "delete_repo",
            "update_last_refresh",
            "update_enable_temporal",
            "update_enable_scip",
            "update_next_refresh",
            "close",
        ]
        for method_name in required:
            assert callable(
                getattr(backend, method_name, None)
            ), f"Missing method: {method_name}"


# ---------------------------------------------------------------------------
# register_repo
# ---------------------------------------------------------------------------


class TestRegisterRepo:
    """Tests for register_repo (INSERT … ON CONFLICT DO UPDATE)."""

    def test_register_repo_executes_upsert(self) -> None:
        """
        Given a mock pool
        When register_repo() is called
        Then cur.execute is called with an INSERT … ON CONFLICT statement.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.register_repo(
            alias_name="my-repo-global",
            repo_name="my-repo",
            repo_url="https://github.com/org/repo.git",
            index_path="/data/index/my-repo",
        )

        mock_cursor.execute.assert_called_once()
        sql = mock_cursor.execute.call_args[0][0]
        assert "INSERT INTO global_repos" in sql
        assert "ON CONFLICT" in sql
        mock_conn.commit.assert_called_once()

    def test_register_repo_passes_correct_params(self) -> None:
        """
        Given specific repo parameters
        When register_repo() is called
        Then the execute params tuple contains all expected values.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.register_repo(
            alias_name="test-alias",
            repo_name="test-repo",
            repo_url="https://example.com/repo.git",
            index_path="/path/to/index",
            enable_temporal=True,
            temporal_options={"days": 30},
            enable_scip=True,
        )

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] == "test-alias"  # alias_name
        assert params[1] == "test-repo"  # repo_name
        assert params[2] == "https://example.com/repo.git"  # repo_url
        assert params[3] == "/path/to/index"  # index_path
        assert params[6] is True  # enable_temporal
        assert json.loads(params[7]) == {"days": 30}  # temporal_options JSON
        assert params[8] is True  # enable_scip

    def test_register_repo_serializes_temporal_options_as_json(self) -> None:
        """
        Given temporal_options dict
        When register_repo() is called
        Then temporal_options is serialized to JSON string in the params.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        opts = {"keep_days": 90, "branches": ["main", "develop"]}
        backend.register_repo("a", "a", None, "/idx", temporal_options=opts)

        params = mock_cursor.execute.call_args[0][1]
        assert json.loads(params[7]) == opts

    def test_register_repo_none_temporal_options_passes_none(self) -> None:
        """
        Given temporal_options=None
        When register_repo() is called
        Then the temporal_options param is None (not JSON-encoded).
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.register_repo("a", "a", None, "/idx")

        params = mock_cursor.execute.call_args[0][1]
        assert params[7] is None


# ---------------------------------------------------------------------------
# get_repo
# ---------------------------------------------------------------------------


class TestGetRepo:
    """Tests for get_repo (SELECT by alias_name)."""

    def test_get_repo_returns_dict_when_found(self) -> None:
        """
        Given a DB row exists for the alias
        When get_repo() is called
        Then a populated dict is returned.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        row = (
            "my-alias",  # alias_name
            "my-repo",  # repo_name
            "https://x.com",  # repo_url
            "/idx",  # index_path
            "2024-01-01T00:00:00+00:00",  # created_at
            "2024-06-01T00:00:00+00:00",  # last_refresh
            True,  # enable_temporal
            '{"days": 30}',  # temporal_options (JSON string)
            False,  # enable_scip
            None,  # next_refresh
        )
        mock_pool, _, _ = _make_mock_pool(fetchone_return=row)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.get_repo("my-alias")

        assert result is not None
        assert result["alias_name"] == "my-alias"
        assert result["enable_temporal"] is True
        assert result["temporal_options"] == {"days": 30}
        assert result["enable_scip"] is False

    def test_get_repo_returns_none_when_not_found(self) -> None:
        """
        Given no DB row for the alias
        When get_repo() is called
        Then None is returned.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchone_return=None)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.get_repo("nonexistent")
        assert result is None

    def test_get_repo_handles_jsonb_dict_temporal_options(self) -> None:
        """
        Given temporal_options comes back as dict (psycopg JSONB parsing)
        When get_repo() is called
        Then temporal_options is returned as dict without double-parsing.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        row = (
            "alias",
            "name",
            None,
            "/idx",
            "2024-01-01T00:00:00+00:00",
            "2024-01-01T00:00:00+00:00",
            False,
            {"key": "val"},  # dict, not string
            False,
            None,
        )
        mock_pool, _, _ = _make_mock_pool(fetchone_return=row)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.get_repo("alias")
        assert result["temporal_options"] == {"key": "val"}

    def test_get_repo_executes_select_with_correct_param(self) -> None:
        """
        Given alias_name "target-repo"
        When get_repo() is called
        Then SELECT is issued with %s param bound to "target-repo".
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool(fetchone_return=None)
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.get_repo("target-repo")

        sql = mock_cursor.execute.call_args[0][0]
        params = mock_cursor.execute.call_args[0][1]
        assert "SELECT" in sql
        assert "WHERE alias_name = %s" in sql
        assert params == ("target-repo",)


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------


class TestListRepos:
    """Tests for list_repos (SELECT all)."""

    def test_list_repos_returns_dict_keyed_by_alias(self) -> None:
        """
        Given two rows in global_repos
        When list_repos() is called
        Then a dict with two entries keyed by alias_name is returned.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        rows = [
            (
                "alias-a",
                "repo-a",
                None,
                "/idx-a",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                False,
                None,
                False,
                None,
            ),
            (
                "alias-b",
                "repo-b",
                "https://b.com",
                "/idx-b",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                True,
                '{"d":7}',
                True,
                "9999",
            ),
        ]
        mock_pool, _, _ = _make_mock_pool(fetchall_return=rows)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.list_repos()

        assert set(result.keys()) == {"alias-a", "alias-b"}
        assert result["alias-b"]["temporal_options"] == {"d": 7}

    def test_list_repos_returns_empty_dict_when_no_repos(self) -> None:
        """
        Given no rows in global_repos
        When list_repos() is called
        Then an empty dict is returned.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchall_return=[])
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.list_repos()
        assert result == {}


# ---------------------------------------------------------------------------
# delete_repo
# ---------------------------------------------------------------------------


class TestDeleteRepo:
    """Tests for delete_repo."""

    def test_delete_repo_returns_true_when_deleted(self) -> None:
        """
        Given rowcount=1 (record existed and was deleted)
        When delete_repo() is called
        Then True is returned.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool(rowcount=1)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.delete_repo("my-alias")

        assert result is True
        mock_conn.commit.assert_called_once()

    def test_delete_repo_returns_false_when_not_found(self) -> None:
        """
        Given rowcount=0 (no record existed)
        When delete_repo() is called
        Then False is returned.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=0)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.delete_repo("nonexistent")
        assert result is False

    def test_delete_repo_executes_delete_sql(self) -> None:
        """
        Given an alias_name
        When delete_repo() is called
        Then DELETE FROM global_repos WHERE alias_name = %s is executed.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.delete_repo("bye-repo")

        sql = mock_cursor.execute.call_args[0][0]
        assert "DELETE FROM global_repos" in sql
        assert "alias_name = %s" in sql


# ---------------------------------------------------------------------------
# update_last_refresh
# ---------------------------------------------------------------------------


class TestUpdateLastRefresh:
    """Tests for update_last_refresh."""

    def test_update_last_refresh_returns_true_when_updated(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool(rowcount=1)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.update_last_refresh("my-alias")
        assert result is True
        mock_conn.commit.assert_called_once()

    def test_update_last_refresh_returns_false_when_not_found(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=0)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.update_last_refresh("nonexistent")
        assert result is False

    def test_update_last_refresh_executes_update_sql(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.update_last_refresh("alias-x")

        sql = mock_cursor.execute.call_args[0][0]
        assert "UPDATE global_repos SET last_refresh" in sql
        assert "%s" in sql


# ---------------------------------------------------------------------------
# update_enable_temporal
# ---------------------------------------------------------------------------


class TestUpdateEnableTemporal:
    """Tests for update_enable_temporal."""

    def test_update_enable_temporal_true_returns_true(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=1)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.update_enable_temporal("alias", True)
        assert result is True

    def test_update_enable_temporal_passes_bool_param(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.update_enable_temporal("alias", False)

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] is False
        assert params[1] == "alias"


# ---------------------------------------------------------------------------
# update_enable_scip
# ---------------------------------------------------------------------------


class TestUpdateEnableScip:
    """Tests for update_enable_scip."""

    def test_update_enable_scip_returns_true_when_updated(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=1)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.update_enable_scip("alias", True)
        assert result is True

    def test_update_enable_scip_executes_correct_sql(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.update_enable_scip("alias", True)

        sql = mock_cursor.execute.call_args[0][0]
        assert "enable_scip" in sql
        assert "UPDATE global_repos" in sql


# ---------------------------------------------------------------------------
# update_next_refresh
# ---------------------------------------------------------------------------


class TestUpdateNextRefresh:
    """Tests for update_next_refresh."""

    def test_update_next_refresh_with_value_returns_true(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=1)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.update_next_refresh("alias", "1234567890")
        assert result is True

    def test_update_next_refresh_with_none_passes_none(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.update_next_refresh("alias", None)

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] is None
        assert params[1] == "alias"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    """Tests for close()."""

    def test_close_delegates_to_pool(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.close()

        mock_pool.close.assert_called_once()
