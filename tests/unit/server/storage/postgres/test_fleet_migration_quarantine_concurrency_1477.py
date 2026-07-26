"""
Issue #1477 Finding L (HIGH, Codex round-6 review): a REAL, live,
unmocked PostgreSQL concurrency test proving
GoldenRepoMetadataPostgresBackend.record_fleet_migration_failure has no
lost-update race.

BACKGROUND: the original implementation issued a separate SELECT to read
the current consecutive_failure_count, computed count + 1 in Python, then
issued a second INSERT/UPDATE with that computed value. Under real
concurrency, two connections can both read the same starting count, both
compute the same next value, and one connection's increment silently
overwrites (loses) the other's. Codex reproduced this with a real,
concurrent, unmocked PostgreSQL connection pair. The fix collapses the
read-then-write into a SINGLE atomic
`INSERT ... ON CONFLICT (golden_alias) DO UPDATE SET
consecutive_failure_count = consecutive_failure_count + 1 ... RETURNING`
statement -- PostgreSQL's own row-level atomicity performs the increment,
so there is no read/write window for a second connection to race into.

Mirrors this project's two established live-PG conventions exactly (never
inventing a new one):
  - TEST_POSTGRES_DSN-gated module-scoped connectivity fixture + per-test
    real-table fixture (test_golden_repo_metadata_temporal_options_live_pg_1414.py).
  - threading.Barrier-forced genuine concurrent race, run across multiple
    iterations to prove determinism, not a single lucky pass
    (test_bug1235_pg_duplicate_claim_race.py's TestRealPgConcurrencyBarrier).

TEST_POSTGRES_DSN is not set in this development environment as of this
writing -- the entire class below is skip-gated and will report "skipped"
rather than "passed" here. It is designed to run for real the moment a
developer points TEST_POSTGRES_DSN at an actual PostgreSQL instance,
exactly like test_bug1235_pg_duplicate_claim_race.py's own live-PG class.
"""

import os
import threading
from typing import Any, List

import pytest

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

# Bounded wait budgets for the barrier-forced concurrency tests below --
# named per Messi Rule #17 (anti-magic) rather than inline literals.
BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 15


@pytest.fixture(scope="module")
def pg_dsn_for_fleet_migration_quarantine():
    """Module-scoped DSN string for the live-PG fleet-migration-quarantine
    concurrency test. Skips cleanly if unavailable (matches
    pg_dsn_for_temporal_options in
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
def fleet_migration_quarantine_state_table(pg_dsn_for_fleet_migration_quarantine):
    """Create a real fleet_migration_quarantine_state table (matching
    040_fleet_migration_quarantine_state.sql exactly) before each test,
    dropped after, for isolation from any other schema/table that may
    exist on the target DB."""
    import psycopg

    with psycopg.connect(
        pg_dsn_for_fleet_migration_quarantine, autocommit=True
    ) as conn:
        conn.execute("DROP TABLE IF EXISTS fleet_migration_quarantine_state")
        conn.execute(
            """
            CREATE TABLE fleet_migration_quarantine_state (
                golden_alias                TEXT PRIMARY KEY,
                consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
                state_signature              TEXT,
                first_failed_at              TIMESTAMPTZ,
                last_failed_at               TIMESTAMPTZ,
                updated_at                   TIMESTAMPTZ,
                signature_checked_at         TIMESTAMPTZ,
                failure_cause                TEXT
            )
            """
        )
    yield pg_dsn_for_fleet_migration_quarantine
    with psycopg.connect(
        pg_dsn_for_fleet_migration_quarantine, autocommit=True
    ) as conn:
        conn.execute("DROP TABLE IF EXISTS fleet_migration_quarantine_state")


def _start_and_join_all(
    threads: List[threading.Thread], *, join_timeout: float
) -> None:
    """Start every thread, join each with a bound, and fail loudly (rather
    than silently continuing to assertions) if any worker is still alive
    after its timed join -- a hung/stalled worker must never be treated as
    an implicit pass."""
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=join_timeout)
    stuck = [t for t in threads if t.is_alive()]
    assert not stuck, (
        f"{len(stuck)} worker thread(s) did not finish within "
        f"{join_timeout}s -- treating this as a test failure rather than "
        "silently inspecting a possibly-incomplete race outcome."
    )


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestRecordFleetMigrationFailureRealPgConcurrency:
    """Real PG: many concurrent record_fleet_migration_failure() calls for
    the SAME golden_alias must never lose an increment."""

    _THREADS_PER_ITERATION = 5
    _ITERATIONS = 10

    def test_concurrent_failures_never_lose_an_increment(
        self, fleet_migration_quarantine_state_table
    ) -> None:
        """Across several barrier-forced concurrent iterations: the final
        persisted consecutive_failure_count always equals the exact number
        of concurrent callers -- proving the atomic ON CONFLICT DO UPDATE
        statement has no lost-update window."""
        import psycopg

        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(
            fleet_migration_quarantine_state_table,
            name="issue1477-concurrency-test",
        )
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            golden_alias = "issue1477-concurrent-repo"

            for iteration in range(self._ITERATIONS):
                # Clean slate for each iteration so every trial starts from
                # zero and the assertion is unambiguous.
                with psycopg.connect(
                    fleet_migration_quarantine_state_table, autocommit=True
                ) as conn:
                    conn.execute(
                        "DELETE FROM fleet_migration_quarantine_state "
                        "WHERE golden_alias = %s",
                        (golden_alias,),
                    )

                errors: List[Exception] = []
                errors_lock = threading.Lock()
                barrier = threading.Barrier(self._THREADS_PER_ITERATION)

                def _record_one(worker_index: int) -> None:
                    try:
                        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                        backend.record_fleet_migration_failure(
                            golden_alias,
                            f"sig-iter-{iteration}-worker-{worker_index}",
                        )
                    except Exception as exc:  # pragma: no cover - failure path
                        with errors_lock:
                            errors.append(exc)

                threads = [
                    threading.Thread(target=_record_one, args=(i,))
                    for i in range(self._THREADS_PER_ITERATION)
                ]
                _start_and_join_all(threads, join_timeout=THREAD_JOIN_TIMEOUT_SECONDS)

                assert not errors, (
                    f"Iteration {iteration}: unexpected exceptions during "
                    f"concurrent record_fleet_migration_failure calls: {errors}"
                )

                final_state = backend.get_fleet_migration_failure_state(golden_alias)
                assert final_state is not None
                assert (
                    final_state["consecutive_failure_count"]
                    == self._THREADS_PER_ITERATION
                ), (
                    f"Iteration {iteration}: lost update detected -- expected "
                    f"consecutive_failure_count == {self._THREADS_PER_ITERATION} "
                    f"(one increment per concurrent caller), got "
                    f"{final_state['consecutive_failure_count']}. This is "
                    "exactly the Finding L lost-update race the atomic "
                    "ON CONFLICT DO UPDATE statement must prevent."
                )
        finally:
            pool.close()

    def test_concurrent_failures_persist_a_consistent_final_write(
        self, fleet_migration_quarantine_state_table
    ) -> None:
        """The row left behind after a concurrent race must reflect exactly
        ONE of the concurrent callers' (state_signature, failure_cause)
        pair -- never a corrupted/partial mix of two writers' values,
        proving the atomic statement's SET clause is not itself subject to
        a torn write."""
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(
            fleet_migration_quarantine_state_table,
            name="issue1477-concurrency-test-2",
        )
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            golden_alias = "issue1477-concurrent-repo-2"
            expected_pairs = {
                ("sig-a", "disk_headroom"),
                ("sig-b", "generic"),
            }
            errors: List[Exception] = []
            errors_lock = threading.Lock()
            barrier = threading.Barrier(len(expected_pairs))

            def _record_pair(signature: str, cause: str) -> None:
                try:
                    barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
                    backend.record_fleet_migration_failure(
                        golden_alias, signature, failure_cause=cause
                    )
                except Exception as exc:  # pragma: no cover - failure path
                    with errors_lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=_record_pair, args=pair)
                for pair in expected_pairs
            ]
            _start_and_join_all(threads, join_timeout=THREAD_JOIN_TIMEOUT_SECONDS)

            assert not errors, f"Unexpected exceptions: {errors}"

            final_state = backend.get_fleet_migration_failure_state(golden_alias)
            assert final_state is not None
            assert final_state["consecutive_failure_count"] == len(expected_pairs)
            observed_pair: Any = (
                final_state["state_signature"],
                final_state["failure_cause"],
            )
            assert observed_pair in expected_pairs, (
                f"Final row {observed_pair} is not one of the writers' own "
                f"pairs {expected_pairs} -- a torn/interleaved write "
                "occurred."
            )
        finally:
            pool.close()


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available")
class TestSoftResetFleetMigrationFailureCountRealPg:
    """Issue #1477 Finding N (Codex round-7 review): a REAL, live
    PostgreSQL round-trip proving `soft_reset_fleet_migration_failure_count`
    genuinely zeroes `consecutive_failure_count` while KEEPING the row --
    the fallback used when the full reset (DELETE) fails but a plain
    UPDATE still works, so a just-repaired repo gets a genuinely fresh
    failure budget instead of resuming from a stale, elevated count."""

    def test_soft_reset_zeroes_count_and_keeps_row_then_next_failure_starts_at_one(
        self, fleet_migration_quarantine_state_table
    ) -> None:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(
            fleet_migration_quarantine_state_table,
            name="issue1477-soft-reset-live-test",
        )
        try:
            backend = GoldenRepoMetadataPostgresBackend(pool)
            golden_alias = "issue1477-soft-reset-repo"

            for _ in range(3):
                backend.record_fleet_migration_failure(golden_alias, "sig-1")

            state_before = backend.get_fleet_migration_failure_state(golden_alias)
            assert state_before is not None
            assert state_before["consecutive_failure_count"] == 3

            backend.soft_reset_fleet_migration_failure_count(golden_alias)

            state_after_soft_reset = backend.get_fleet_migration_failure_state(
                golden_alias
            )
            assert state_after_soft_reset is not None, (
                "soft reset must KEEP the row against real PostgreSQL -- "
                "unlike reset_fleet_migration_failure, which deletes it"
            )
            assert state_after_soft_reset["consecutive_failure_count"] == 0
            # The signature/first_failed_at bookkeeping is untouched by a
            # soft reset -- only the count is zeroed.
            assert state_after_soft_reset["state_signature"] == "sig-1"

            backend.record_fleet_migration_failure(golden_alias, "sig-2")
            state_after_next_failure = backend.get_fleet_migration_failure_state(
                golden_alias
            )
            assert state_after_next_failure is not None
            assert state_after_next_failure["consecutive_failure_count"] == 1, (
                "Finding N: the failure after a soft reset must start "
                "counting from a genuinely fresh budget (1), never resume "
                "from the stale elevated count (would be 4 without the fix)."
            )
        finally:
            pool.close()
