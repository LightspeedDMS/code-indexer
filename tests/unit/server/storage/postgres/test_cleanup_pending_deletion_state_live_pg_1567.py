"""Bug #1567: live-PostgreSQL round-trip tests for
GoldenRepoMetadataPostgresBackend's durable pending-deletion queue
(schedule_cleanup_deletion/list_cleanup_pending_deletions/
remove_cleanup_pending_deletion).

Mirrors test_fleet_migration_dedup_state_live_pg_1560.py's exact pattern:
gated by TEST_POSTGRES_DSN, skips cleanly when no PostgreSQL is available
(this project's existing posture -- these tests are not run in CI, only
locally against a real instance), creates the real
cleanup_pending_deletion_state table matching migration 047
(047_cleanup_pending_deletion_state.sql) exactly, drops it afterward.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection -- not a mock -- so a silent no-op write cannot be
mistaken for a passing test.
"""

import os
from contextlib import contextmanager

import pytest


HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@contextmanager
def _backend(dsn: str, name: str):
    """Open a fresh ConnectionPool + GoldenRepoMetadataPostgresBackend
    against *dsn*, closing the pool afterward. A FRESH pool/backend per
    call (never shared across writer/reader in a test) proves state is
    genuinely persisted server-side, not cached in-process."""
    pool = ConnectionPool(dsn, name=name)
    try:
        yield GoldenRepoMetadataPostgresBackend(pool)
    finally:
        pool.close()


@pytest.fixture(scope="module")
def pg_dsn_for_cleanup_pending_deletion_state():
    """Module-scoped DSN string for live-PG cleanup-queue tests. Skips if
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
def cleanup_pending_deletion_state_table(pg_dsn_for_cleanup_pending_deletion_state):
    """Create a real cleanup_pending_deletion_state table (matching
    047_cleanup_pending_deletion_state.sql exactly) before each test,
    dropped after, for isolation from any other schema/table that may
    exist on the target DB."""
    dsn = pg_dsn_for_cleanup_pending_deletion_state
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS cleanup_pending_deletion_state")
        conn.execute(
            """
            CREATE TABLE cleanup_pending_deletion_state (
                index_path      TEXT PRIMARY KEY,
                scheduled_at    DOUBLE PRECISION NOT NULL
            )
            """
        )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS cleanup_pending_deletion_state")


pytestmark = pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available"
)


def test_schedule_then_list_round_trips_through_a_new_instance(
    cleanup_pending_deletion_state_table,
) -> None:
    """Write via one pool/backend instance, read back via a BRAND-NEW
    pool/backend instance -- proves the state is genuinely persisted
    server-side, not cached in-process (simulates a process restart
    across cluster nodes)."""
    dsn = cleanup_pending_deletion_state_table

    with _backend(dsn, "bug1567-live-write") as write_backend:
        returned = write_backend.schedule_cleanup_deletion(
            "/versioned/live-repo/v_1000", 1000.0
        )
        assert returned == 1000.0

    with _backend(dsn, "bug1567-live-read") as read_backend:
        rows = read_backend.list_cleanup_pending_deletions()
        assert rows == [
            {"index_path": "/versioned/live-repo/v_1000", "scheduled_at": 1000.0}
        ]


def test_schedule_is_idempotent_and_preserves_original_scheduled_at_live(
    cleanup_pending_deletion_state_table,
) -> None:
    dsn = cleanup_pending_deletion_state_table

    with _backend(dsn, "bug1567-live-idempotent") as backend:
        first = backend.schedule_cleanup_deletion("/versioned/live-repo/v_2000", 2000.0)
        second = backend.schedule_cleanup_deletion(
            "/versioned/live-repo/v_2000", 9999.0
        )
        assert first == 2000.0
        assert second == 2000.0


def test_remove_deletes_the_row_live(
    cleanup_pending_deletion_state_table,
) -> None:
    dsn = cleanup_pending_deletion_state_table

    with _backend(dsn, "bug1567-live-remove") as backend:
        backend.schedule_cleanup_deletion("/versioned/live-repo/v_3000", 3000.0)
        backend.remove_cleanup_pending_deletion("/versioned/live-repo/v_3000")
        assert backend.list_cleanup_pending_deletions() == []


def test_remove_is_idempotent_when_absent_live(
    cleanup_pending_deletion_state_table,
) -> None:
    dsn = cleanup_pending_deletion_state_table

    with _backend(dsn, "bug1567-live-remove-absent") as backend:
        backend.remove_cleanup_pending_deletion("/versioned/never-scheduled")
        assert backend.list_cleanup_pending_deletions() == []
