"""Bug #1533: live-PostgreSQL proof that the temporal lineage lookup reads the
SHARED store.

This is the cluster fact pattern verbatim, against a REAL psycopg connection
and a REAL ``activated_repos`` table -- no mock of the registry, per this
project's "faithful DB mocks" lesson (an unfaithful fake can certify a silent
no-op as passing):

    PG activated rows: [('e2e1529mine', 'e2e1529', 'admin')]
    node-local sqlite activated_repos: 0

The activation row is inserted ONLY into PostgreSQL, and NO node-local JSON
metadata file is written. The activation's clone DIRECTORY does exist, exactly
as on a real node with shared storage -- so the ONLY thing distinguishing a
correct read from the pre-fix one is which metadata store is consulted.

Gated by TEST_POSTGRES_DSN, mirroring
test_golden_repo_metadata_temporal_options_live_pg_1414.py's
pg_dsn_for_runner/isolated-table convention exactly (never inventing a new
one). It skips cleanly where no PostgreSQL is available.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.services.temporal_worker import (
    _resolve_golden_temporal_context,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

USERNAME = "admin"
ACTIVATED_ALIAS = "e2e1533mine"
GOLDEN_ALIAS = "e2e1533"
CLONE_BRANCH = "master"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 2


class _WorkerInput:
    """Only the attributes the lineage resolver reads."""

    def __init__(self, repo_path: str) -> None:
        self.username = USERNAME
        self.repository_alias = ACTIVATED_ALIAS
        self.repo_path = repo_path


@pytest.fixture(scope="module")
def pg_dsn_for_lineage():
    """Module-scoped DSN for the live-PG lineage test. Skips if unavailable."""
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
def activated_repos_table(pg_dsn_for_lineage):
    """A real ``activated_repos`` table matching migrations 011 + 039 exactly,
    created before and dropped after each test for isolation."""
    import psycopg

    with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS activated_repos")
        conn.execute(
            """
            CREATE TABLE activated_repos (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                user_alias TEXT NOT NULL,
                golden_repo_alias TEXT,
                repo_path TEXT NOT NULL,
                current_branch TEXT DEFAULT 'main',
                activated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                last_accessed TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                git_committer_email TEXT,
                ssh_key_used BOOLEAN DEFAULT FALSE,
                is_composite BOOLEAN DEFAULT FALSE,
                wiki_enabled BOOLEAN DEFAULT FALSE,
                metadata_json JSONB,
                activation_id TEXT,
                UNIQUE(username, user_alias)
            )
            """
        )
    yield pg_dsn_for_lineage
    with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS activated_repos")


def _insert_shared_activation_row(dsn: str, repo_path: Path) -> None:
    """Write the activation to the SHARED store ONLY -- no JSON file anywhere,
    which is precisely a cluster node's view of a repo activated elsewhere."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activated_repos "
            "(username, user_alias, golden_repo_alias, repo_path, "
            "current_branch) VALUES (%s, %s, %s, %s, %s)",
            (USERNAME, ACTIVATED_ALIAS, GOLDEN_ALIAS, str(repo_path), CLONE_BRANCH),
        )


@pytest.fixture
def cluster_node_managers(activated_repos_table, tmp_path):
    """Two REAL managers over the SAME data dir, differing only in which
    metadata store they read: one wired to a real psycopg pool (the server's
    DI-wired instance on a cluster node), one not (what the worker used to
    construct for itself). The clone directory exists for both.

    The pool is closed in ``finally`` so a failure anywhere in the remaining
    setup cannot leak it.
    """
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    data_dir = tmp_path / "cluster-node" / "data"

    pg_backed = ActivatedRepoManager(data_dir=str(data_dir))
    pool = ConnectionPool(
        activated_repos_table, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE
    )
    try:
        pg_backed.set_connection_pool(pool)
        node_local = ActivatedRepoManager(data_dir=str(data_dir))

        clone_dir = Path(pg_backed.get_activated_repo_path(USERNAME, ACTIVATED_ALIAS))
        clone_dir.mkdir(parents=True, exist_ok=True)
        _insert_shared_activation_row(activated_repos_table, clone_dir)

        yield pg_backed, node_local, clone_dir
    finally:
        pool.close()


def test_shared_store_manager_resolves_the_lineage(cluster_node_managers) -> None:
    """The row lives only in PostgreSQL, so only the shared-store manager can
    answer -- and it must yield the golden repo's FIXED temporal root."""
    pg_backed, _node_local, clone_dir = cluster_node_managers

    assert pg_backed.uses_shared_metadata_store() is True

    ctx = _resolve_golden_temporal_context(
        _WorkerInput(str(clone_dir)), "job-pg-1533", activated_repo_manager=pg_backed
    )

    assert ctx.alias == GOLDEN_ALIAS
    assert ctx.temporal_index_dir == server_temporal_index_root(
        Path(pg_backed.activated_repos_dir).parent / "golden-repos", GOLDEN_ALIAS
    )


def test_node_local_manager_cannot_see_the_shared_row(cluster_node_managers) -> None:
    """Discriminating counterpart: the same lookup through a node-local
    manager finds NOTHING for a genuinely-activated repo.

    Pre-fix that None became an all-None context, the read fell back to the
    activation's own CoW clone (empty of temporal data since Bug #1529), and
    the query answered HTTP 200 with zero results.
    """
    _pg_backed, node_local, _clone_dir = cluster_node_managers

    assert node_local.uses_shared_metadata_store() is False
    assert node_local.get_repository(USERNAME, ACTIVATED_ALIAS, touch=False) is None
