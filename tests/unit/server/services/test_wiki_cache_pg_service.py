"""
Unit tests for WikiCachePGService.

Story #429: Wiki Cache Migration to PostgreSQL

Tests:
- get_cached_page returns dict with rendered_html/source_hash/cached_at
- get_cached_page returns None when row not found
- cache_page executes UPSERT with correct parameters
- invalidate_repo deletes all rows for the repo and returns count
- is_stale returns True when page not cached
- is_stale returns True when source_hash differs from cached
- is_stale returns False when source_hash matches cached

All tests use mocked connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Return (mock_pool, mock_conn, mock_cursor) wired together."""
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 0
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return mock_pool, mock_conn, mock_cursor


def _make_service(pool=None):
    from code_indexer.server.services.wiki_cache_pg_service import WikiCachePGService

    if pool is None:
        pool, _, _ = _make_pool_and_conn()
    return WikiCachePGService(pool)


# ---------------------------------------------------------------------------
# Tests: get_cached_page
# ---------------------------------------------------------------------------


class TestGetCachedPage:
    def test_returns_dict_when_row_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "<h1>Hello</h1>",
            "sha256:abc",
            "2024-01-01T00:00:00+00:00",
        )
        svc = _make_service(pool)

        result = svc.get_cached_page("my-repo", "docs/index.md")

        assert result is not None
        assert result["rendered_html"] == "<h1>Hello</h1>"
        assert result["source_hash"] == "sha256:abc"
        assert result["cached_at"] == "2024-01-01T00:00:00+00:00"

    def test_returns_none_when_row_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        svc = _make_service(pool)

        result = svc.get_cached_page("my-repo", "docs/missing.md")

        assert result is None

    def test_executes_correct_query_with_params(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        svc = _make_service(pool)

        svc.get_cached_page("repo-alias", "path/to/page.md")

        execute_call = conn.execute.call_args
        sql = execute_call[0][0]
        params = execute_call[0][1]
        assert "wiki_cache" in sql
        assert "WHERE" in sql
        assert params == ("repo-alias", "path/to/page.md")


# ---------------------------------------------------------------------------
# Tests: cache_page
# ---------------------------------------------------------------------------


class TestCachePage:
    def test_executes_upsert(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        svc = _make_service(pool)

        svc.cache_page("my-repo", "docs/index.md", "<h1>Hi</h1>", "sha256:def")

        assert conn.execute.called
        execute_call = conn.execute.call_args
        sql = execute_call[0][0]
        assert "INSERT INTO wiki_cache" in sql
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql

    def test_upsert_params_contain_correct_values(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        svc = _make_service(pool)

        svc.cache_page("repo-x", "wiki/home.md", "<p>content</p>", "sha256:xyz")

        execute_call = conn.execute.call_args
        params = execute_call[0][1]
        # params: (repo_alias, page_path, rendered_html, source_hash, cached_at)
        assert params[0] == "repo-x"
        assert params[1] == "wiki/home.md"
        assert params[2] == "<p>content</p>"
        assert params[3] == "sha256:xyz"
        # cached_at is a timestamp string — just verify it's non-empty
        assert params[4]

    def test_cache_page_does_not_raise_on_success(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        svc = _make_service(pool)

        # Should return None without raising
        result = svc.cache_page("repo", "page.md", "<html/>", "hash123")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: invalidate_repo
# ---------------------------------------------------------------------------


class TestInvalidateRepo:
    def test_returns_row_count(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 5
        svc = _make_service(pool)

        count = svc.invalidate_repo("my-repo")

        assert count == 5

    def test_returns_zero_when_nothing_deleted(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        svc = _make_service(pool)

        count = svc.invalidate_repo("empty-repo")

        assert count == 0

    def test_executes_delete_with_correct_alias(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 3
        svc = _make_service(pool)

        svc.invalidate_repo("target-repo")

        execute_call = conn.execute.call_args
        sql = execute_call[0][0]
        params = execute_call[0][1]
        assert "DELETE FROM wiki_cache" in sql
        assert params == ("target-repo",)


# ---------------------------------------------------------------------------
# Tests: is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_returns_true_when_not_cached(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        svc = _make_service(pool)

        assert svc.is_stale("my-repo", "page.md", "sha256:current") is True

    def test_returns_true_when_hash_differs(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "<html/>",
            "sha256:old",
            "2024-01-01T00:00:00+00:00",
        )
        svc = _make_service(pool)

        assert svc.is_stale("my-repo", "page.md", "sha256:new") is True

    def test_returns_false_when_hash_matches(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "<html/>",
            "sha256:same",
            "2024-01-01T00:00:00+00:00",
        )
        svc = _make_service(pool)

        assert svc.is_stale("my-repo", "page.md", "sha256:same") is False

    def test_is_stale_uses_get_cached_page_internally(self) -> None:
        """is_stale should reuse get_cached_page logic (no duplicate SQL)."""
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "<html/>",
            "sha256:abc",
            "2024-01-01T00:00:00+00:00",
        )
        svc = _make_service(pool)

        # Calling is_stale should result in exactly one execute call (from get_cached_page)
        svc.is_stale("repo", "page.md", "sha256:abc")
        assert conn.execute.call_count == 1
