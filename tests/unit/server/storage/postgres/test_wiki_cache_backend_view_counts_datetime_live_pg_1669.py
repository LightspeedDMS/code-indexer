"""
Bug #1669: live-PostgreSQL round-trip tests for
WikiCachePostgresBackend.get_all_view_counts()'s datetime normalization.

Confirmed live (real 2-node PostgreSQL cluster): the wiki_article_analytics
MCP tool failed 100% of the time in cluster mode with "Object of type
datetime is not JSON serializable". Root cause: get_all_view_counts()
returned first_viewed_at/last_viewed_at as raw psycopg-deserialized
`datetime` objects (wiki_article_views.first_viewed_at/last_viewed_at are
TIMESTAMPTZ columns per migration 024), while the SQLite path returns
ISO-8601 strings (written via datetime.utcnow().isoformat()).

Mirrors test_fleet_migration_dedup_state_live_pg_1560.py's exact pattern:
gated by TEST_POSTGRES_DSN, skips cleanly when no PostgreSQL is available
(this project's existing posture -- these tests are not run in CI, only
locally against a real instance), creates the real wiki_article_views
table matching migration 024 (024_wiki_article_views.sql) exactly, drops
it afterward.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection -- not a mock -- so the TIMESTAMPTZ-to-datetime
deserialization behavior that mocks would hide is genuinely exercised.
"""

import os
from contextlib import contextmanager

import pytest


HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.wiki_cache_backend import (
        WikiCachePostgresBackend,
    )

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@contextmanager
def _backend(dsn: str, name: str):
    """Open a fresh ConnectionPool + WikiCachePostgresBackend against *dsn*,
    closing the pool afterward. A FRESH pool/backend per call (never shared
    across writer/reader in a test) proves state is genuinely persisted
    server-side, not cached in-process."""
    pool = ConnectionPool(dsn, name=name)
    try:
        yield WikiCachePostgresBackend(pool)
    finally:
        pool.close()


@pytest.fixture(scope="module")
def pg_dsn_for_wiki_view_counts():
    """Module-scoped DSN string for live-PG wiki view-count tests. Skips if
    unavailable (matches pg_dsn_for_dedup_state in
    test_fleet_migration_dedup_state_live_pg_1560.py)."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def wiki_article_views_table(pg_dsn_for_wiki_view_counts):
    """Create a real wiki_article_views table (matching
    024_wiki_article_views.sql exactly) before each test, dropped after,
    for isolation from any other schema/table that may exist on the
    target DB."""
    dsn = pg_dsn_for_wiki_view_counts
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS wiki_article_views")
        conn.execute(
            """
            CREATE TABLE wiki_article_views (
                repo_alias TEXT NOT NULL,
                article_path TEXT NOT NULL,
                real_views INTEGER DEFAULT 0,
                first_viewed_at TIMESTAMP WITH TIME ZONE,
                last_viewed_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (repo_alias, article_path)
            )
            """
        )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS wiki_article_views")


pytestmark = pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available"
)


class TestGetAllViewCountsDatetimeContract:
    """Core Bug #1669 reproduction: datetime-vs-string return contract."""

    def test_returns_string_not_datetime_for_first_and_last_viewed_at(
        self, wiki_article_views_table
    ) -> None:
        """On real PostgreSQL, first_viewed_at/last_viewed_at come back
        from psycopg as `datetime` objects for TIMESTAMPTZ columns. The
        fix must convert them to ISO-8601 strings matching the SQLite
        path's contract."""
        dsn = wiki_article_views_table

        with _backend(dsn, "bug1669-live-write") as write_backend:
            write_backend.increment_view(
                "my-repo", "guide.md", "2026-08-24T12:00:00+00:00"
            )

        with _backend(dsn, "bug1669-live-read") as read_backend:
            rows = read_backend.get_all_view_counts("my-repo")

        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row["first_viewed_at"], str), (
            f"expected str, got {type(row['first_viewed_at'])}"
        )
        assert isinstance(row["last_viewed_at"], str), (
            f"expected str, got {type(row['last_viewed_at'])}"
        )

    def test_returned_rows_are_json_serializable(
        self, wiki_article_views_table
    ) -> None:
        """Direct reproduction of the reported symptom: the
        wiki_article_analytics MCP tool crashed with "Object of type
        datetime is not JSON serializable" when serializing this method's
        return value."""
        import json

        dsn = wiki_article_views_table

        with _backend(dsn, "bug1669-live-json-write") as write_backend:
            write_backend.increment_view(
                "my-repo", "guide.md", "2026-08-24T12:00:00+00:00"
            )

        with _backend(dsn, "bug1669-live-json-read") as read_backend:
            rows = read_backend.get_all_view_counts("my-repo")

        # Must not raise TypeError: Object of type datetime is not JSON serializable
        serialized = json.dumps(rows)
        assert "guide.md" in serialized


class TestGetAllViewCountsBehaviorUnaffectedByFix:
    """Confirms the fix does not change any other field's value/behavior."""

    def test_multiple_views_preserve_real_views_count(
        self, wiki_article_views_table
    ) -> None:
        dsn = wiki_article_views_table

        with _backend(dsn, "bug1669-live-multi") as backend:
            backend.increment_view("my-repo", "guide.md", "2026-08-24T12:00:00+00:00")
            backend.increment_view("my-repo", "guide.md", "2026-08-24T13:00:00+00:00")
            rows = backend.get_all_view_counts("my-repo")

        assert len(rows) == 1
        assert rows[0]["real_views"] == 2
        assert isinstance(rows[0]["first_viewed_at"], str)
        assert isinstance(rows[0]["last_viewed_at"], str)

    def test_empty_repo_returns_empty_list(self, wiki_article_views_table) -> None:
        dsn = wiki_article_views_table

        with _backend(dsn, "bug1669-live-empty") as backend:
            rows = backend.get_all_view_counts("no-such-repo")

        assert rows == []
