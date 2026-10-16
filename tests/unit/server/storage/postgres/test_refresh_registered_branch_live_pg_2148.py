"""Bug #2148 on a live PostgreSQL cluster store.

A cluster node's per-node cidx_server.db golden_repos_metadata table is empty
or stale; the registration wrote the registered branch to PostgreSQL. The
lifespan wires that PostgreSQL backend into the RefreshScheduler
(``golden_repo_metadata_backend=backend_registry.golden_repo_metadata``). In
``storage_mode=postgres`` a real refresh must keep the base clone on the
branch registered there and never switch it to the remote default
(origin/HEAD = main). When PostgreSQL has no row, the refresh switches
nothing -- even when the per-node SQLite file holds a stale row -- logs a
WARNING naming the alias, and continues on the clone's current branch.

Gated by TEST_POSTGRES_DSN (skips cleanly without it). The module gets a
database of its own, migrated by the real MigrationRunner (conftest
``migrated_scratch_pg_dsn``). Real git repositories in tmp dirs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator, Tuple

import pytest

from tests.unit.global_repos.test_refresh_git_cancel_2012 import (
    make_git_repo_scheduler,
)
from tests.unit.global_repos.test_refresh_registered_branch_2148 import (  # noqa: F401
    assert_refresh_continues_on_current_branch,
    current_branch,
    make_feature_branch_clone,
    per_node_sqlite,
    refresh,
    register_branch,
    server_storage_mode,
)

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@pytest.fixture
def pg_metadata(migrated_scratch_pg_dsn: str) -> Iterator[Any]:
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )

    pool = ConnectionPool(migrated_scratch_pg_dsn, name="registered-branch-2148")
    try:
        yield GoldenRepoMetadataPostgresBackend(pool)
    finally:
        pool.close()
        with psycopg.connect(migrated_scratch_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM refresh_failure_backoff_state")
            conn.execute("DELETE FROM golden_repos_metadata")


def _cluster_scheduler(
    tmp_path: Path,
    origin: Path,
    master: Path,
    pg: Any,
    set_storage_mode: Callable[..., None],
) -> Tuple[Any, Any]:
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    per_node_sqlite(master)  # the per-node table exists with zero rows
    scheduler.golden_repo_metadata = pg  # the lifespan's cluster wiring
    # The backend registry supplies the same PostgreSQL store.
    set_storage_mode("postgres", pg)
    return scheduler, recorder


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestRegisteredBranchFromClusterStore:
    def test_registered_non_default_branch_is_kept(
        self,
        tmp_path: Path,
        pg_metadata: Any,
        server_storage_mode: Callable[[str], None],  # noqa: F811
    ) -> None:
        origin, master = make_feature_branch_clone(tmp_path)
        register_branch(pg_metadata, origin, master, "feature-x")
        scheduler, _ = _cluster_scheduler(
            tmp_path, origin, master, pg_metadata, server_storage_mode
        )

        refresh(scheduler)

        assert current_branch(master) == "feature-x"

    def test_clone_on_wrong_branch_is_corrected_to_registered_branch(
        self,
        tmp_path: Path,
        pg_metadata: Any,
        server_storage_mode: Callable[[str], None],  # noqa: F811
    ) -> None:
        origin, master = make_feature_branch_clone(tmp_path)
        register_branch(pg_metadata, origin, master, "main")
        scheduler, _ = _cluster_scheduler(
            tmp_path, origin, master, pg_metadata, server_storage_mode
        )

        refresh(scheduler)

        assert current_branch(master) == "main"

    def test_no_row_in_cluster_store_warns_and_continues_without_switch(
        self,
        tmp_path: Path,
        pg_metadata: Any,
        server_storage_mode: Callable[[str], None],  # noqa: F811
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """PostgreSQL has no row; the per-node SQLite row (stale, 'main') is
        never consulted: the clone stays on feature-x and the refresh pulls."""
        origin, master = make_feature_branch_clone(tmp_path)
        scheduler, _ = _cluster_scheduler(
            tmp_path, origin, master, pg_metadata, server_storage_mode
        )
        register_branch(per_node_sqlite(master), origin, master, "main")

        assert_refresh_continues_on_current_branch(
            tmp_path, master, scheduler, caplog, "no golden repo metadata row"
        )
