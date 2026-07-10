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
from datetime import datetime, timezone
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
            "list_due_repos",
            "close",
        ]
        for method_name in required:
            assert callable(getattr(backend, method_name, None)), (
                f"Missing method: {method_name}"
            )


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
                datetime.fromtimestamp(9999.0, tz=timezone.utc),
            ),
        ]
        mock_pool, _, _ = _make_mock_pool(fetchall_return=rows)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.list_repos()

        assert set(result.keys()) == {"alias-a", "alias-b"}
        assert result["alias-b"]["temporal_options"] == {"d": 7}
        assert result["alias-b"]["next_refresh"] == "9999.0", (
            "next_refresh must round-trip to the epoch-float-STRING contract "
            f"the scheduler/GlobalRegistry expects, got: "
            f"{result['alias-b']['next_refresh']!r}"
        )

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

    def test_update_next_refresh_with_value_converts_epoch_string_to_utc_datetime(
        self,
    ) -> None:
        """
        Blocker B (Bug #1308 re-review): next_refresh is TIMESTAMPTZ, not TEXT
        (src/code_indexer/server/storage/postgres/migrations/sql/001_initial_schema.sql:82).
        The scheduler's contract (via PostgresGlobalRegistryAdapter) passes an
        epoch-float STRING (matching the SQLite backend's TEXT-column
        semantics) -- the PG backend must convert that string to a
        timezone-aware UTC datetime before binding it to the TIMESTAMPTZ
        column, not write the naked string (which would error against a real
        PG connection).
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.update_next_refresh("alias", "1700000000.5")

        params = mock_cursor.execute.call_args[0][1]
        assert isinstance(params[0], datetime), (
            f"Expected a datetime bound to the TIMESTAMPTZ column, got: {params[0]!r}"
        )
        assert params[0].tzinfo == timezone.utc, (
            f"next_refresh datetime must be UTC, got tzinfo={params[0].tzinfo!r}"
        )
        assert params[0].timestamp() == 1700000000.5
        assert params[1] == "alias"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# list_due_repos (Bug #1308 remediation item #2)
# ---------------------------------------------------------------------------


class TestListDueRepos:
    """
    Tests for list_due_repos (Bug #1308): mirrors the SQLite backend's
    oldest-first, capped due-query (Bug #1063 semantics) so the cluster
    RefreshScheduler can auto-refresh in postgres mode.
    """

    def test_list_due_repos_returns_empty_list_when_limit_zero(self) -> None:
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.list_due_repos(limit=0, now=1000.0)

        assert result == []
        mock_cursor.execute.assert_not_called()

    def test_list_due_repos_executes_select_with_native_timestamptz_comparison(
        self,
    ) -> None:
        """
        Blocker B (Bug #1308 re-review): next_refresh is TIMESTAMPTZ, not TEXT.
        `CAST(next_refresh AS DOUBLE PRECISION)` is an invalid query-plan
        against a timestamptz column and would fail EVERY list_due_repos()
        call against a real PG connection. The comparison must be NATIVE
        timestamptz (via to_timestamp(%s) on the bound epoch float), never a
        cast of the column itself.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool(fetchall_return=[])
        backend = GlobalReposPostgresBackend(mock_pool)

        backend.list_due_repos(limit=5, now=1700000000.0)

        sql = mock_cursor.execute.call_args[0][0]
        params = mock_cursor.execute.call_args[0][1]
        assert "next_refresh IS NOT NULL" in sql
        assert "ORDER BY" in sql
        assert "LIMIT %s" in sql
        assert "to_timestamp(%s)" in sql, (
            f"Expected a native to_timestamp(%s) comparison, got SQL: {sql}"
        )
        assert "DOUBLE PRECISION" not in sql.upper(), (
            f"CAST(...AS DOUBLE PRECISION) is invalid against a TIMESTAMPTZ "
            f"column (Blocker B); got SQL: {sql}"
        )
        assert params == (1700000000.0, 5)

    def test_list_due_repos_returns_rows_as_dicts_oldest_first(self) -> None:
        """
        psycopg returns a real `datetime` for a TIMESTAMPTZ column -- the
        fixture rows must reflect that (not raw epoch strings) so this test
        actually exercises the datetime->epoch-string conversion that
        _row_to_dict must perform to preserve the SQLite backend's
        epoch-float-string contract for next_refresh.
        """
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )

        next_refresh_a = datetime.fromtimestamp(100.0, tz=timezone.utc)
        next_refresh_b = datetime.fromtimestamp(200.0, tz=timezone.utc)
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
                next_refresh_a,
            ),
            (
                "alias-b",
                "repo-b",
                "https://b.com",
                "/idx-b",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                True,
                '{"d": 7}',
                True,
                next_refresh_b,
            ),
        ]
        mock_pool, _, _ = _make_mock_pool(fetchall_return=rows)
        backend = GlobalReposPostgresBackend(mock_pool)

        result = backend.list_due_repos(limit=10, now=1000.0)

        assert len(result) == 2
        assert result[0]["alias_name"] == "alias-a"
        assert result[1]["alias_name"] == "alias-b"
        assert result[1]["temporal_options"] == {"d": 7}
        assert result[1]["enable_scip"] is True
        assert result[0]["next_refresh"] == "100.0", (
            "next_refresh must round-trip to the epoch-float-STRING contract "
            f"the scheduler/GlobalRegistry expects, got: {result[0]['next_refresh']!r}"
        )
        assert result[1]["next_refresh"] == "200.0"


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
