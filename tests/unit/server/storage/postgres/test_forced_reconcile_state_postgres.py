"""Durable per-repo forced-reconcile state, PostgreSQL mirror of
tests/unit/server/storage/test_forced_reconcile_state_sqlite.py, against a
real database migrated by the real MigrationRunner (skipped without
TEST_POSTGRES_DSN)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterator, List

import pytest

from tests.unit.server.storage.test_forced_reconcile_state_sqlite import (
    run_forced_reconcile_state_scenario,
)

if TYPE_CHECKING:
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )


@pytest.fixture
def open_backend(
    migrated_scratch_pg_dsn: str,
) -> Iterator[Callable[[], "GoldenRepoMetadataPostgresBackend"]]:
    """Factory of backends (each with its own pool); all closed at teardown."""
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )

    opened: List[GoldenRepoMetadataPostgresBackend] = []

    def factory() -> GoldenRepoMetadataPostgresBackend:
        backend = GoldenRepoMetadataPostgresBackend(
            ConnectionPool(migrated_scratch_pg_dsn)
        )
        opened.append(backend)
        return backend

    try:
        yield factory
    finally:
        try:
            if opened:
                with opened[0]._pool.connection() as conn:
                    conn.execute("DELETE FROM golden_repos_metadata")
                    conn.execute("DELETE FROM forced_reconcile_state")
        finally:
            for backend in opened:
                backend.close()


def test_forced_reconcile_state_postgres(
    open_backend: Callable[[], "GoldenRepoMetadataPostgresBackend"],
) -> None:
    backend = open_backend()

    def register_repo() -> None:
        backend.add_repo(
            alias="example-repo",
            repo_url="https://example.com/example-repo.git",
            default_branch="main",
            clone_path="/srv/example/example-repo",
            created_at="2026-01-01T00:00:00+00:00",
        )

    run_forced_reconcile_state_scenario(backend, open_backend, register_repo)
