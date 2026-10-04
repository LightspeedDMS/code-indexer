"""
Bug #1414 DoD item 5: live-PostgreSQL round-trip test for
GoldenRepoMetadataPostgresBackend.update_temporal_options.

Gated by TEST_POSTGRES_DSN (skips cleanly when no PostgreSQL is available;
these tests are not run in CI, only locally against a real PostgreSQL).
The module gets a database of its own on that server, migrated by the real
MigrationRunner (conftest ``migrated_scratch_pg_dsn``), so the tests run
against the REAL migrated golden_repos_metadata table and never DROP/CREATE
a table in the shared database (which fails once that database is migrated:
other tables hold foreign keys to golden_repos_metadata).

Per the project's "faithful DB mocks" lesson (mock-based tests can certify
a silent no-op write as passing if the mock doesn't mirror the real driver),
this test exercises a REAL psycopg v3 connection against a REAL
golden_repos_metadata table -- not a mock -- to prove the write actually
persists and round-trips.
"""

from typing import Iterator

import pytest


HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@pytest.fixture
def golden_repos_metadata_table(migrated_scratch_pg_dsn: str) -> Iterator[str]:
    """The real, migrated golden_repos_metadata table in this module's own
    database; emptied after each test."""
    import psycopg

    yield migrated_scratch_pg_dsn
    with psycopg.connect(migrated_scratch_pg_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM golden_repos_metadata")


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
