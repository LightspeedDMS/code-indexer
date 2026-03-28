"""
Unit tests for GoldenRepoMetadataPostgresBackend.

Story #412: PostgreSQL Backend for GlobalRepos and GoldenRepoMetadata

All tests use a mocked ConnectionPool — no real PostgreSQL required.
Verifies:
  - Protocol compliance (isinstance check against GoldenRepoMetadataBackend)
  - All method signatures match the Protocol
  - SQL generation and parameterization are correct for all 3 tables
  - Return values match expected semantics
  - Cross-table operations (invalidation methods) issue correct SQL
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
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()

    @contextmanager
    def _connection():
        yield mock_conn

    mock_pool.connection.side_effect = _connection

    return mock_pool, mock_conn, mock_cursor


def _make_mock_pool_sequential(side_effects):
    """
    Build a mock ConnectionPool where each call to connection() uses a fresh
    mock conn+cursor from the provided list of (fetchone, fetchall, rowcount)
    tuples.
    """
    conns = []
    for fetchone, fetchall, rowcount in side_effects:
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = fetchone
        mock_cursor.fetchall.return_value = fetchall or []
        mock_cursor.rowcount = rowcount

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        conns.append((mock_conn, mock_cursor))

    idx = [0]

    @contextmanager
    def _connection():
        i = idx[0]
        idx[0] += 1
        yield conns[i][0]

    mock_pool = MagicMock()
    mock_pool.connection.side_effect = _connection
    return mock_pool, conns


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestGoldenRepoMetadataProtocolCompliance:
    """Verify GoldenRepoMetadataPostgresBackend satisfies the Protocol."""

    def test_isinstance_check_passes(self) -> None:
        """
        Given GoldenRepoMetadataPostgresBackend is instantiated with a mock pool
        When isinstance(backend, GoldenRepoMetadataBackend) is called
        Then it returns True (structural subtyping via @runtime_checkable).
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend

        mock_pool, _, _ = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert isinstance(backend, GoldenRepoMetadataBackend)

    def test_has_all_required_methods(self) -> None:
        """
        Given GoldenRepoMetadataPostgresBackend is instantiated
        When the Protocol's required methods are checked
        Then all are present as callables.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        required = [
            "ensure_table_exists",
            "add_repo",
            "get_repo",
            "list_repos",
            "remove_repo",
            "repo_exists",
            "update_enable_temporal",
            "update_repo_url",
            "update_category",
            "update_wiki_enabled",
            "update_default_branch",
            "invalidate_description_refresh_tracking",
            "invalidate_dependency_map_tracking",
            "list_repos_with_categories",
            "close",
        ]
        for method_name in required:
            assert callable(getattr(backend, method_name, None)), (
                f"Missing method: {method_name}"
            )


# ---------------------------------------------------------------------------
# ensure_table_exists
# ---------------------------------------------------------------------------


class TestEnsureTableExists:
    """Tests for ensure_table_exists (no-op in Postgres)."""

    def test_ensure_table_exists_is_noop(self) -> None:
        """
        Given Postgres backend
        When ensure_table_exists() is called
        Then no database connection is acquired (DDL handled by migration runner).
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.ensure_table_exists()

        mock_pool.connection.assert_not_called()


# ---------------------------------------------------------------------------
# add_repo
# ---------------------------------------------------------------------------


class TestAddRepo:
    """Tests for add_repo (INSERT)."""

    def test_add_repo_executes_insert(self) -> None:
        """
        Given a mock pool
        When add_repo() is called
        Then an INSERT INTO golden_repos_metadata statement is executed.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.add_repo(
            alias="my-golden-repo",
            repo_url="https://github.com/org/repo.git",
            default_branch="main",
            clone_path="/data/golden-repos/my-golden-repo",
            created_at="2024-01-01T00:00:00+00:00",
        )

        mock_cursor.execute.assert_called_once()
        sql = mock_cursor.execute.call_args[0][0]
        assert "INSERT INTO golden_repos_metadata" in sql
        mock_conn.commit.assert_called_once()

    def test_add_repo_passes_correct_params(self) -> None:
        """
        Given specific repo parameters
        When add_repo() is called
        Then the execute params contain all expected values.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        opts = {"keep_days": 60}
        backend.add_repo(
            alias="gr-test",
            repo_url="https://x.com/r.git",
            default_branch="develop",
            clone_path="/clones/gr-test",
            created_at="2024-03-01T12:00:00+00:00",
            enable_temporal=True,
            temporal_options=opts,
        )

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] == "gr-test"
        assert params[1] == "https://x.com/r.git"
        assert params[2] == "develop"
        assert params[3] == "/clones/gr-test"
        assert params[4] == "2024-03-01T12:00:00+00:00"
        assert params[5] is True
        assert json.loads(params[6]) == opts

    def test_add_repo_none_temporal_options_passes_none(self) -> None:
        """
        Given temporal_options=None
        When add_repo() is called
        Then temporal_options param is None.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.add_repo("a", "url", "main", "/p", "2024-01-01T00:00:00+00:00")

        params = mock_cursor.execute.call_args[0][1]
        assert params[6] is None


# ---------------------------------------------------------------------------
# get_repo
# ---------------------------------------------------------------------------


class TestGetRepo:
    """Tests for get_repo (SELECT by alias)."""

    def test_get_repo_returns_dict_when_found(self) -> None:
        """
        Given a DB row exists for the alias
        When get_repo() is called
        Then a populated dict with all fields is returned.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        row = (
            "gr-alias",  # alias
            "https://x.com/r.git",  # repo_url
            "main",  # default_branch
            "/clones/gr-alias",  # clone_path
            "2024-01-01T00:00:00+00:00",  # created_at
            True,  # enable_temporal
            '{"days": 30}',  # temporal_options
            42,  # category_id
            False,  # category_auto_assigned
            True,  # wiki_enabled
        )
        mock_pool, _, _ = _make_mock_pool(fetchone_return=row)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        result = backend.get_repo("gr-alias")

        assert result is not None
        assert result["alias"] == "gr-alias"
        assert result["enable_temporal"] is True
        assert result["temporal_options"] == {"days": 30}
        assert result["category_id"] == 42
        assert result["category_auto_assigned"] is False
        assert result["wiki_enabled"] is True

    def test_get_repo_returns_none_when_not_found(self) -> None:
        """
        Given no DB row for the alias
        When get_repo() is called
        Then None is returned.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.get_repo("missing") is None

    def test_get_repo_handles_jsonb_dict_temporal_options(self) -> None:
        """
        Given temporal_options returned as dict (psycopg v3 JSONB auto-parsing)
        When get_repo() is called
        Then temporal_options in the result is the dict, not double-parsed.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        row = (
            "a",
            "u",
            "main",
            "/p",
            "2024-01-01T00:00:00+00:00",
            False,
            {"key": "val"},  # dict, not string
            None,
            False,
            False,
        )
        mock_pool, _, _ = _make_mock_pool(fetchone_return=row)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        result = backend.get_repo("a")
        assert result["temporal_options"] == {"key": "val"}

    def test_get_repo_executes_select_with_correct_param(self) -> None:
        """
        Given alias "test-golden"
        When get_repo() is called
        Then SELECT … WHERE alias = %s is executed with that alias.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.get_repo("test-golden")

        sql = mock_cursor.execute.call_args[0][0]
        params = mock_cursor.execute.call_args[0][1]
        assert "WHERE alias = %s" in sql
        assert params == ("test-golden",)


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------


class TestListRepos:
    """Tests for list_repos (SELECT all, basic columns)."""

    def test_list_repos_returns_list_of_dicts(self) -> None:
        """
        Given two rows in golden_repos_metadata
        When list_repos() is called
        Then a list of two dicts is returned (no category fields).
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        rows = [
            (
                "a1",
                "url1",
                "main",
                "/p1",
                "2024-01-01T00:00:00+00:00",
                False,
                None,
                False,
            ),
            (
                "a2",
                "url2",
                "dev",
                "/p2",
                "2024-02-01T00:00:00+00:00",
                True,
                '{"d":7}',
                True,
            ),
        ]
        mock_pool, _, _ = _make_mock_pool(fetchall_return=rows)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        result = backend.list_repos()

        assert len(result) == 2
        assert result[0]["alias"] == "a1"
        assert result[1]["temporal_options"] == {"d": 7}
        assert result[1]["wiki_enabled"] is True

    def test_list_repos_returns_empty_list_when_no_repos(self) -> None:
        """
        Given no rows
        When list_repos() is called
        Then an empty list is returned.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchall_return=[])
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.list_repos() == []


# ---------------------------------------------------------------------------
# remove_repo
# ---------------------------------------------------------------------------


class TestRemoveRepo:
    """Tests for remove_repo (DELETE)."""

    def test_remove_repo_returns_true_when_deleted(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, mock_conn, _ = _make_mock_pool(rowcount=1)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        result = backend.remove_repo("gr-alias")
        assert result is True
        mock_conn.commit.assert_called_once()

    def test_remove_repo_returns_false_when_not_found(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=0)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        result = backend.remove_repo("nonexistent")
        assert result is False

    def test_remove_repo_executes_delete_sql(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.remove_repo("target")

        sql = mock_cursor.execute.call_args[0][0]
        assert "DELETE FROM golden_repos_metadata" in sql
        assert "alias = %s" in sql


# ---------------------------------------------------------------------------
# repo_exists
# ---------------------------------------------------------------------------


class TestRepoExists:
    """Tests for repo_exists."""

    def test_repo_exists_returns_true_when_row_found(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchone_return=(1,))
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.repo_exists("exists") is True

    def test_repo_exists_returns_false_when_no_row(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.repo_exists("missing") is False


# ---------------------------------------------------------------------------
# update_enable_temporal
# ---------------------------------------------------------------------------


class TestUpdateEnableTemporal:
    """Tests for update_enable_temporal."""

    def test_update_enable_temporal_returns_true_when_updated(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=1)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.update_enable_temporal("alias", True) is True

    def test_update_enable_temporal_passes_bool_param(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.update_enable_temporal("my-alias", False)

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] is False
        assert params[1] == "my-alias"


# ---------------------------------------------------------------------------
# update_repo_url
# ---------------------------------------------------------------------------


class TestUpdateRepoUrl:
    """Tests for update_repo_url."""

    def test_update_repo_url_executes_update_sql(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool(rowcount=1)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.update_repo_url("my-alias", "https://new.url/repo.git")

        sql = mock_cursor.execute.call_args[0][0]
        params = mock_cursor.execute.call_args[0][1]
        assert "repo_url" in sql
        assert params[0] == "https://new.url/repo.git"
        assert params[1] == "my-alias"

    def test_update_repo_url_returns_true_when_updated(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=1)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.update_repo_url("alias", "url") is True

    def test_update_repo_url_returns_false_when_not_found(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=0)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.update_repo_url("missing", "url") is False


# ---------------------------------------------------------------------------
# update_category
# ---------------------------------------------------------------------------


class TestUpdateCategory:
    """Tests for update_category."""

    def test_update_category_passes_category_id_and_auto_assigned(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool(rowcount=1)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.update_category("alias", category_id=7, auto_assigned=False)

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] == 7
        assert params[1] is False
        assert params[2] == "alias"

    def test_update_category_none_category_id_passes_none(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, mock_cursor = _make_mock_pool(rowcount=1)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.update_category("alias", category_id=None)

        params = mock_cursor.execute.call_args[0][1]
        assert params[0] is None

    def test_update_category_returns_false_when_not_found(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(rowcount=0)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.update_category("missing", None) is False


# ---------------------------------------------------------------------------
# update_wiki_enabled
# ---------------------------------------------------------------------------


class TestUpdateWikiEnabled:
    """Tests for update_wiki_enabled (returns None per Protocol)."""

    def test_update_wiki_enabled_executes_update_sql(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.update_wiki_enabled("alias", True)

        sql = mock_cursor.execute.call_args[0][0]
        assert "wiki_enabled" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert params[0] is True
        assert params[1] == "alias"
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# update_default_branch
# ---------------------------------------------------------------------------


class TestUpdateDefaultBranch:
    """Tests for update_default_branch (returns None per Protocol)."""

    def test_update_default_branch_executes_update_sql(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.update_default_branch("alias", "feature/x")

        sql = mock_cursor.execute.call_args[0][0]
        assert "default_branch" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert params[0] == "feature/x"
        assert params[1] == "alias"
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# invalidate_description_refresh_tracking
# ---------------------------------------------------------------------------


class TestInvalidateDescriptionRefreshTracking:
    """Tests for invalidate_description_refresh_tracking (cross-table UPDATE)."""

    def test_invalidates_correct_table(self) -> None:
        """
        Given an alias
        When invalidate_description_refresh_tracking() is called
        Then UPDATE description_refresh_tracking SET last_known_commit = NULL is executed.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.invalidate_description_refresh_tracking("my-alias")

        sql = mock_cursor.execute.call_args[0][0]
        assert "description_refresh_tracking" in sql
        assert "last_known_commit" in sql
        assert "NULL" in sql
        params = mock_cursor.execute.call_args[0][1]
        assert params == ("my-alias",)
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# invalidate_dependency_map_tracking
# ---------------------------------------------------------------------------


class TestInvalidateDependencyMapTracking:
    """Tests for invalidate_dependency_map_tracking (cross-table JSON mutation)."""

    def test_removes_alias_from_commit_hashes(self) -> None:
        """
        Given commit_hashes JSON contains the alias
        When invalidate_dependency_map_tracking() is called
        Then the alias key is removed and the updated JSON is written back.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        hashes = {"alias-to-remove": "abc123", "other-alias": "def456"}
        # First call: SELECT; second call: UPDATE
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (json.dumps(hashes),)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()

        @contextmanager
        def _connection():
            yield mock_conn

        mock_pool.connection.side_effect = _connection

        backend = GoldenRepoMetadataPostgresBackend(mock_pool)
        backend.invalidate_dependency_map_tracking("alias-to-remove")

        # The second execute call should be an UPDATE with the alias removed
        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2

        update_sql = calls[1][0][0]
        assert "UPDATE dependency_map_tracking" in update_sql

        update_params = calls[1][0][1]
        updated_hashes = json.loads(update_params[0])
        assert "alias-to-remove" not in updated_hashes
        assert updated_hashes == {"other-alias": "def456"}

        mock_conn.commit.assert_called_once()

    def test_noop_when_no_tracking_record(self) -> None:
        """
        Given no row in dependency_map_tracking
        When invalidate_dependency_map_tracking() is called
        Then no UPDATE is issued.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, mock_conn, mock_cursor = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.invalidate_dependency_map_tracking("any-alias")

        # Only the SELECT should have been called — no UPDATE
        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 1
        assert "SELECT" in calls[0][0][0]

    def test_noop_when_alias_not_in_commit_hashes(self) -> None:
        """
        Given commit_hashes does not contain the alias
        When invalidate_dependency_map_tracking() is called
        Then no UPDATE is issued.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        hashes = {"other-alias": "abc"}
        mock_pool, mock_conn, mock_cursor = _make_mock_pool(
            fetchone_return=(json.dumps(hashes),)
        )
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.invalidate_dependency_map_tracking("not-in-hashes")

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 1  # only SELECT, no UPDATE

    def test_handles_jsonb_dict_commit_hashes(self) -> None:
        """
        Given commit_hashes returned as dict (psycopg JSONB auto-parse)
        When invalidate_dependency_map_tracking() is called
        Then the alias is removed from the dict and written back as JSON.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        hashes_dict = {"alias-x": "hash1", "alias-y": "hash2"}
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (hashes_dict,)  # dict, not string

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()

        @contextmanager
        def _connection():
            yield mock_conn

        mock_pool.connection.side_effect = _connection

        backend = GoldenRepoMetadataPostgresBackend(mock_pool)
        backend.invalidate_dependency_map_tracking("alias-x")

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2
        update_params = calls[1][0][1]
        updated = json.loads(update_params[0])
        assert updated == {"alias-y": "hash2"}


# ---------------------------------------------------------------------------
# list_repos_with_categories
# ---------------------------------------------------------------------------


class TestListReposWithCategories:
    """Tests for list_repos_with_categories (SELECT with all columns)."""

    def test_list_repos_with_categories_includes_category_fields(self) -> None:
        """
        Given rows with category_id and category_auto_assigned
        When list_repos_with_categories() is called
        Then dicts include those fields.
        """
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        rows = [
            (
                "alias1",
                "url1",
                "main",
                "/p1",
                "2024-01-01T00:00:00+00:00",
                False,
                None,
                5,
                True,
                False,
            ),
        ]
        mock_pool, _, _ = _make_mock_pool(fetchall_return=rows)
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        result = backend.list_repos_with_categories()

        assert len(result) == 1
        assert result[0]["category_id"] == 5
        assert result[0]["category_auto_assigned"] is True
        assert "wiki_enabled" in result[0]

    def test_list_repos_with_categories_returns_empty_list_when_no_repos(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool(fetchall_return=[])
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        assert backend.list_repos_with_categories() == []


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    """Tests for close()."""

    def test_close_delegates_to_pool(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        mock_pool, _, _ = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(mock_pool)

        backend.close()

        mock_pool.close.assert_called_once()
