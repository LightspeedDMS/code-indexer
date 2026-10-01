"""
Story #1560 (coordinator Gap 2 + AC7): live-PostgreSQL round-trip tests
for GoldenRepoMetadataPostgresBackend's duplicate-point-id
auto-resolution outcome state (record_dedup_outcome/get_dedup_state/
list_dedup_states/clear_dedup_state).

Mirrors test_golden_repo_metadata_temporal_options_live_pg_1414.py's
exact pattern: gated by TEST_POSTGRES_DSN, skips cleanly when no
PostgreSQL is available (this project's existing posture -- these tests
are not run in CI, only locally against a real instance), creates the
real fleet_migration_dedup_state table matching migration 045
(045_fleet_migration_dedup_state.sql) exactly, drops it afterward.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection -- not a mock -- so a silent no-op write cannot be
mistaken for a passing test. AC7 explicitly requires this coverage on
BOTH backends; the SQLite side already has it in
test_fleet_migration_dedup_state_1560.py.
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
def pg_dsn_for_dedup_state():
    """Module-scoped DSN string for live-PG dedup-state tests. Skips if
    unavailable (matches pg_dsn_for_temporal_options in
    test_golden_repo_metadata_temporal_options_live_pg_1414.py)."""
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
def dedup_state_table(pg_dsn_for_dedup_state):
    """Create a real fleet_migration_dedup_state table (matching
    045_fleet_migration_dedup_state.sql exactly) before each test,
    dropped after, for isolation from any other schema/table that may
    exist on the target DB."""
    import psycopg

    with psycopg.connect(pg_dsn_for_dedup_state, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS fleet_migration_dedup_state")
        conn.execute(
            """
            CREATE TABLE fleet_migration_dedup_state (
                golden_alias                TEXT PRIMARY KEY,
                duplicate_groups            INTEGER NOT NULL DEFAULT 0,
                records_before               INTEGER NOT NULL DEFAULT 0,
                records_deleted              INTEGER NOT NULL DEFAULT 0,
                winner_kept_groups           INTEGER NOT NULL DEFAULT 0,
                whole_group_deleted_groups   INTEGER NOT NULL DEFAULT 0,
                collection_total             INTEGER NOT NULL DEFAULT 0,
                first_dropped_at             TIMESTAMPTZ,
                dropped_at                   TIMESTAMPTZ,
                cleared_at                   TIMESTAMPTZ,
                cleared_reason               TEXT
            )
            """
        )
    yield pg_dsn_for_dedup_state
    with psycopg.connect(pg_dsn_for_dedup_state, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS fleet_migration_dedup_state")


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestDedupStateLivePostgres:
    """Real round-trip through a live PostgreSQL connection for every
    dedup-state method on GoldenRepoMetadataPostgresBackend."""

    def test_record_then_get_round_trips_through_a_new_instance(
        self, dedup_state_table
    ) -> None:
        """AC7: write via one pool/backend instance, read back via a
        BRAND-NEW pool/backend instance -- proves the state is genuinely
        persisted server-side, not cached in-process."""
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        write_pool = ConnectionPool(dedup_state_table, name="story1560-live-write")
        try:
            write_backend = GoldenRepoMetadataPostgresBackend(write_pool)
            written = write_backend.record_dedup_outcome(
                "story1560-live-repo",
                duplicate_groups=33,
                records_before=343604,
                records_deleted=43,
                winner_kept_groups=23,
                whole_group_deleted_groups=10,
                collection_total=343604,
            )
            assert written["records_deleted"] == 43
        finally:
            write_pool.close()

        read_pool = ConnectionPool(dedup_state_table, name="story1560-live-read")
        try:
            read_backend = GoldenRepoMetadataPostgresBackend(read_pool)
            fetched = read_backend.get_dedup_state("story1560-live-repo")
            assert fetched is not None
            assert fetched["duplicate_groups"] == 33
            assert fetched["records_deleted"] == 43
            assert fetched["winner_kept_groups"] == 23
            assert fetched["whole_group_deleted_groups"] == 10
            assert fetched["collection_total"] == 343604
            assert fetched["cleared_at"] is None
        finally:
            read_pool.close()

    def test_list_dedup_states_includes_recorded_alias_live(
        self, dedup_state_table
    ) -> None:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(dedup_state_table, name="story1560-live-list")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            backend.record_dedup_outcome(
                "story1560-live-list-repo",
                duplicate_groups=1,
                records_before=10,
                records_deleted=1,
                winner_kept_groups=1,
                whole_group_deleted_groups=0,
                collection_total=10,
            )

            aliases = {row["golden_alias"] for row in backend.list_dedup_states()}
            assert aliases == {"story1560-live-list-repo"}
        finally:
            pool.close()

    def test_repeated_outcome_accumulates_cumulative_fields_live(
        self, dedup_state_table
    ) -> None:
        """AC9: duplicate_groups/records_deleted/winner_kept_groups/
        whole_group_deleted_groups ADD across passes server-side (the
        atomic ON CONFLICT ... DO UPDATE SET x = table.x + EXCLUDED.x),
        while records_before/collection_total OVERWRITE as the latest
        snapshot."""
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(dedup_state_table, name="story1560-live-cumulative")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            backend.record_dedup_outcome(
                "story1560-live-cumulative-repo",
                duplicate_groups=5,
                records_before=100,
                records_deleted=5,
                winner_kept_groups=3,
                whole_group_deleted_groups=2,
                collection_total=100,
            )
            backend.record_dedup_outcome(
                "story1560-live-cumulative-repo",
                duplicate_groups=2,
                records_before=95,
                records_deleted=2,
                winner_kept_groups=1,
                whole_group_deleted_groups=1,
                collection_total=95,
            )

            state = backend.get_dedup_state("story1560-live-cumulative-repo")
            assert state is not None
            assert state["duplicate_groups"] == 7
            assert state["records_deleted"] == 7
            assert state["winner_kept_groups"] == 4
            assert state["whole_group_deleted_groups"] == 3
            assert state["records_before"] == 95
            assert state["collection_total"] == 95
        finally:
            pool.close()

    def test_clear_marks_cleared_and_excludes_from_active_state_live(
        self, dedup_state_table
    ) -> None:
        """AC8: clear_dedup_state stamps cleared_at/cleared_reason; a
        subsequent record_dedup_outcome call reactivates the row
        (cleared_at reset to NULL) since a fresh outcome is active
        again."""
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(dedup_state_table, name="story1560-live-clear")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            backend.record_dedup_outcome(
                "story1560-live-clear-repo",
                duplicate_groups=1,
                records_before=10,
                records_deleted=1,
                winner_kept_groups=1,
                whole_group_deleted_groups=0,
                collection_total=10,
            )

            backend.clear_dedup_state(
                "story1560-live-clear-repo", "successful full re-index"
            )
            cleared_state = backend.get_dedup_state("story1560-live-clear-repo")
            assert cleared_state is not None
            assert cleared_state["cleared_at"] is not None
            assert cleared_state["cleared_reason"] == "successful full re-index"

            backend.record_dedup_outcome(
                "story1560-live-clear-repo",
                duplicate_groups=1,
                records_before=9,
                records_deleted=1,
                winner_kept_groups=1,
                whole_group_deleted_groups=0,
                collection_total=9,
            )
            reactivated_state = backend.get_dedup_state("story1560-live-clear-repo")
            assert reactivated_state is not None
            assert reactivated_state["cleared_at"] is None
            assert reactivated_state["cleared_reason"] is None
        finally:
            pool.close()

    def test_get_dedup_state_returns_none_when_never_recorded_live(
        self, dedup_state_table
    ) -> None:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(dedup_state_table, name="story1560-live-absent")
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            assert backend.get_dedup_state("never-recorded-alias") is None
        finally:
            pool.close()
