"""GoldenRepoMetadataPostgresBackend.existing_aliases against a live
PostgreSQL (public #1984).

Gated by TEST_POSTGRES_DSN (skips cleanly without it). The module gets a
database of its own, migrated by the real MigrationRunner (conftest
``migrated_scratch_pg_dsn``), so the lookup runs against the real
golden_repos_metadata table.
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
def backend(migrated_scratch_pg_dsn: str) -> Iterator[object]:
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )

    pool = ConnectionPool(migrated_scratch_pg_dsn, name="existing-aliases-live")
    try:
        yield GoldenRepoMetadataPostgresBackend(pool)
    finally:
        pool.close()
        with psycopg.connect(migrated_scratch_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM golden_repos_metadata")


def _add(backend, alias: str) -> None:
    backend.add_repo(
        alias=alias,
        repo_url=f"https://git.example.com/example/{alias}.git",
        default_branch="main",
        clone_path=f"/data/golden-repos/{alias}",
        created_at="2024-01-01T00:00:00+00:00",
    )


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestExistingAliasesLivePostgres:
    def test_returns_only_the_requested_names_that_exist(self, backend) -> None:
        for alias in ("example-repo", "second-repo", "other-repo"):
            _add(backend, alias)

        found = backend.existing_aliases(["example-repo", "my-alias", "other-repo"])

        assert found == {"example-repo", "other-repo"}

    def test_empty_request_returns_empty_set(self, backend) -> None:
        _add(backend, "example-repo")

        assert backend.existing_aliases([]) == set()

    def test_large_request_is_answered_in_full(self, backend) -> None:
        for i in range(5):
            _add(backend, f"repo-{i}")
        names = [f"missing-{i}" for i in range(2500)] + ["repo-1", "repo-4"]

        assert backend.existing_aliases(names) == {"repo-1", "repo-4"}
