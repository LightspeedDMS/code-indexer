"""
Bug #1414 DoD item 5: live-PostgreSQL round-trip test for
GoldenRepoMetadataPostgresBackend.update_temporal_options.

Mirrors the pg_dsn_for_runner / isolated_schema live-PG pattern already
established in test_migration_runner.py (Story #1164) exactly -- gated by
TEST_POSTGRES_DSN, skips cleanly when no PostgreSQL is available (matching
this project's existing CI posture: these tests are not run in CI, only
locally when a developer has a real PostgreSQL instance to point at).

Per the project's "faithful DB mocks" lesson (mock-based tests can certify
a silent no-op write as passing if the mock doesn't mirror the real driver),
this test exercises a REAL psycopg v3 connection against a REAL
golden_repos_metadata table -- not a mock -- to prove the write actually
persists and round-trips.
"""

import os

import pytest


HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn_for_temporal_options():
    """Module-scoped DSN string for live-PG temporal_options tests. Skips
    if unavailable (matches pg_dsn_for_runner in test_migration_runner.py)."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def golden_repos_metadata_table(pg_dsn_for_temporal_options):
    """Create a real golden_repos_metadata table (matching
    001_initial_schema.sql exactly) before each test, dropped after, for
    isolation from any other schema/table that may exist on the target DB."""
    import psycopg

    with psycopg.connect(pg_dsn_for_temporal_options, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS golden_repos_metadata")
        conn.execute(
            """
            CREATE TABLE golden_repos_metadata (
                alias                   TEXT        PRIMARY KEY NOT NULL,
                repo_url                TEXT        NOT NULL,
                default_branch          TEXT        NOT NULL,
                clone_path              TEXT        NOT NULL,
                created_at              TIMESTAMPTZ NOT NULL,
                enable_temporal         BOOLEAN     NOT NULL DEFAULT FALSE,
                temporal_options        JSONB,
                wiki_enabled            BOOLEAN     DEFAULT FALSE,
                category_id             INTEGER,
                category_auto_assigned  BOOLEAN     DEFAULT FALSE
            )
            """
        )
    yield pg_dsn_for_temporal_options
    with psycopg.connect(pg_dsn_for_temporal_options, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS golden_repos_metadata")


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestUpdateTemporalOptionsLivePostgres:
    """Bug #1414: real round-trip through a live PostgreSQL connection --
    write via update_temporal_options, read back via get_repo, assert the
    exact dict is returned (not silently dropped/no-op'd)."""

    def test_update_temporal_options_persists_and_round_trips(
        self, golden_repos_metadata_table
    ) -> None:
        from datetime import datetime, timezone

        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(golden_repos_metadata_table, name="bug1414-live-test")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            backend.add_repo(
                alias="bug1414-live-repo",
                repo_url="https://github.com/org/repo.git",
                default_branch="main",
                clone_path="/data/golden-repos/bug1414-live-repo",
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            edited_options = {
                "max_commits": 250,
                "since_date": "2024-06-01",
                "diff_context": 4,
                "all_branches": True,
            }
            updated = backend.update_temporal_options(
                "bug1414-live-repo", edited_options
            )
            assert updated is True

            fetched = backend.get_repo("bug1414-live-repo")
            assert fetched is not None
            assert fetched["temporal_options"] == edited_options, (
                "Bug #1414: update_temporal_options write did not persist/"
                f"round-trip correctly through real PostgreSQL. Got: {fetched}"
            )
        finally:
            pool.close()

    def test_update_temporal_options_none_clears_column_live(
        self, golden_repos_metadata_table
    ) -> None:
        from datetime import datetime, timezone

        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(golden_repos_metadata_table, name="bug1414-live-test-2")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            backend.add_repo(
                alias="bug1414-live-repo-2",
                repo_url="https://github.com/org/repo2.git",
                default_branch="main",
                clone_path="/data/golden-repos/bug1414-live-repo-2",
                created_at=datetime.now(timezone.utc).isoformat(),
                temporal_options={"max_commits": 10},
            )

            updated = backend.update_temporal_options("bug1414-live-repo-2", None)
            assert updated is True

            fetched = backend.get_repo("bug1414-live-repo-2")
            assert fetched is not None
            assert fetched["temporal_options"] is None
        finally:
            pool.close()

    def test_update_temporal_options_returns_false_for_missing_alias_live(
        self, golden_repos_metadata_table
    ) -> None:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(golden_repos_metadata_table, name="bug1414-live-test-3")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            assert (
                backend.update_temporal_options(
                    "does-not-exist-1414", {"max_commits": 1}
                )
                is False
            )
        finally:
            pool.close()
