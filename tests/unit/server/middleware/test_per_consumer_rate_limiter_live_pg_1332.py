"""
Story: PR #1332 review fix -- LIVE PostgreSQL proof of cluster-shared
per-consumer admission-control rate limiting.

Gated by TEST_POSTGRES_DSN (same convention as
test_migration_runner.py::TestAdvisoryLockConcurrentLivePG) -- skipped when
no real PostgreSQL is reachable. This is intentionally a faithful, non-mocked
test: two PerConsumerRateLimiter instances share ONE real
code_indexer.server.storage.postgres.connection_pool.ConnectionPool
(simulating two uvicorn workers/nodes), proving the combined consumption
against a single consumer is bounded by the configured capacity -- NOT
capacity x 2. This is the exact fleet-wide guarantee PR #1332's docstring
claimed but never implemented (PerConsumerRateLimiter never called
set_connection_pool()).

Authorization header values below are synthetic test-fixture placeholder
strings (not real secrets) used only to derive distinct SHA-256 consumer
keys for the test scenarios.
"""

import os

import pytest

from code_indexer.server.middleware.admission_control import PerConsumerRateLimiter

HAS_PSYCOPG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn():
    """Module-scoped DSN string for live-PG tests. Skips if unavailable."""
    if not HAS_PSYCOPG:
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
def isolated_consumer_table(pg_dsn):
    """Fresh consumer_rate_limit_state table for each test, dropped after."""
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS consumer_rate_limit_state")
        conn.execute(
            """
            CREATE TABLE consumer_rate_limit_state (
                consumer_key TEXT             PRIMARY KEY,
                tokens       DOUBLE PRECISION NOT NULL DEFAULT 10.0,
                last_refill  DOUBLE PRECISION NOT NULL,
                last_access  DOUBLE PRECISION NOT NULL
            )
            """
        )
    yield pg_dsn
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS consumer_rate_limit_state")


class _Req:
    """Minimal request stand-in. ``auth`` is a synthetic test-fixture string
    (not a real secret) used only to derive a distinct SHA-256 consumer key."""

    def __init__(self, auth: str = "Bearer test-fixture-shared-node-value"):
        self.headers = {"authorization": auth}
        self.cookies: dict = {}


@pytest.mark.skipif(not HAS_PSYCOPG, reason="psycopg not available")
class TestLivePGCrossNodeFleetWideSharing:
    def test_two_worker_instances_share_one_real_pg_bucket(
        self, isolated_consumer_table
    ) -> None:
        """Two PerConsumerRateLimiter instances (simulating two uvicorn
        workers/nodes) sharing ONE real PostgreSQL connection pool must
        enforce a SINGLE combined bucket for the same consumer credential --
        combined allowed count == capacity, never 2x capacity."""
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        pool = ConnectionPool(isolated_consumer_table, min_size=1, max_size=4)
        try:
            worker_a = PerConsumerRateLimiter(capacity=3, refill_per_second=0.0)
            worker_a.set_connection_pool(pool)
            worker_b = PerConsumerRateLimiter(capacity=3, refill_per_second=0.0)
            worker_b.set_connection_pool(pool)

            req = _Req()
            results = []
            # Round-robin 6 requests across the two "workers" -- if buckets
            # were per-process (the bug), all 6 would be allowed (3 each).
            # With genuine PG sharing, only 3 total are allowed.
            for i in range(6):
                worker = worker_a if i % 2 == 0 else worker_b
                allowed, _ = worker.check(req)
                results.append(allowed)

            assert results.count(True) == 3, (
                f"Expected exactly capacity=3 allowed across BOTH workers "
                f"combined (fleet-wide sharing), got {results.count(True)}: "
                f"{results}"
            )
            assert results.count(False) == 3
        finally:
            pool.close()

    def test_different_consumers_do_not_interfere_on_real_pg(
        self, isolated_consumer_table
    ) -> None:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        pool = ConnectionPool(isolated_consumer_table, min_size=1, max_size=4)
        try:
            worker_a = PerConsumerRateLimiter(capacity=1, refill_per_second=0.0)
            worker_a.set_connection_pool(pool)
            worker_b = PerConsumerRateLimiter(capacity=1, refill_per_second=0.0)
            worker_b.set_connection_pool(pool)

            req_first = _Req(auth="Bearer test-fixture-consumer-one")
            req_second = _Req(auth="Bearer test-fixture-consumer-two")

            allowed_first, _ = worker_a.check(req_first)
            allowed_second, _ = worker_b.check(req_second)

            assert allowed_first is True
            assert allowed_second is True  # independent consumer, own bucket
        finally:
            pool.close()

    def test_writes_never_land_in_token_bucket_state_on_real_pg(
        self, isolated_consumer_table
    ) -> None:
        """Even against real PostgreSQL, the consumer limiter must never
        touch/create the auth login-limiter's token_bucket_state table."""
        import psycopg
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        pool = ConnectionPool(isolated_consumer_table, min_size=1, max_size=4)
        try:
            worker = PerConsumerRateLimiter(capacity=5, refill_per_second=1.0)
            worker.set_connection_pool(pool)
            worker.check(_Req())
        finally:
            pool.close()

        with psycopg.connect(isolated_consumer_table) as conn:
            _exists_row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'token_bucket_state')"
            ).fetchone()
            assert _exists_row is not None  # SELECT EXISTS always returns one row
            table_exists = _exists_row[0]
        # token_bucket_state may or may not exist depending on whether the
        # auth migration ran in this DB; either way, this limiter must not
        # be the thing that created it, and it must have zero rows if it
        # does exist as a leftover from another test/migration run.
        if table_exists:
            with psycopg.connect(isolated_consumer_table) as conn:
                rows = conn.execute(
                    "SELECT * FROM token_bucket_state WHERE username = %s",
                    ("test-fixture-shared-node-value",),
                ).fetchall()
            assert rows == []
