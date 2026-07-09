"""
Bug #1334: TokenBucketManager._pg_consume atomic conditional decrement.

The PostgreSQL-backed token-bucket rate limiters performed their decrement as
a non-atomic ``SELECT tokens ...; UPDATE tokens = tokens - cost ...`` guarded
only by a per-process ``threading.Lock``. Under simultaneous cross-node
bursts on the SAME key, two nodes could both read ``tokens >= cost`` and both
decrement, allowing a bounded overshoot beyond the configured capacity. This
affects BOTH PG-backed limiters sharing ``TokenBucketManager._pg_consume``:
the auth login limiter (``token_bucket_state``/``username``) and the
admission-control ``PerConsumerRateLimiter`` (``consumer_rate_limit_state``/
``consumer_key``, added in #1332).

This module proves, against REAL PostgreSQL (gated by TEST_POSTGRES_DSN, same
convention as test_per_consumer_rate_limiter_live_pg_1332.py):

1. A concurrency test that hammers ONE key from many threads, each with its
   OWN TokenBucketManager + OWN ConnectionPool (simulating separate
   node/worker processes, each with its own independent per-process lock --
   exactly the scenario the per-process lock cannot protect). Total allowed
   across ALL threads must equal EXACTLY the configured capacity -- zero
   overshoot. Parametrized over BOTH production table/key-column pairs so
   both limiters are proven, not just one.
2. A refill-preservation test proving the atomic path's inline SQL refill
   arithmetic (elapsed-time refill + capacity clamp) matches the original
   Python arithmetic exactly.
"""

from __future__ import annotations

import os
import threading

import pytest

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


def _isolated_table(pg_dsn: str, table_name: str, key_column: str):
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.execute(
            f"""
            CREATE TABLE {table_name} (
                {key_column} TEXT             PRIMARY KEY,
                tokens       DOUBLE PRECISION NOT NULL DEFAULT 10.0,
                last_refill  DOUBLE PRECISION NOT NULL,
                last_access  DOUBLE PRECISION NOT NULL
            )
            """
        )


def _drop_table(pg_dsn: str, table_name: str) -> None:
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")


@pytest.mark.skipif(not HAS_PSYCOPG, reason="psycopg not available")
class TestPgConsumeAtomicConcurrency:
    """Real-PG proof that concurrent cross-node bursts on ONE key never
    exceed the configured capacity -- zero overshoot, not "bounded by
    concurrency". Parametrized over both production limiter tables."""

    @pytest.mark.parametrize(
        "table_name,key_column",
        [
            ("token_bucket_state", "username"),  # auth login limiter
            ("consumer_rate_limit_state", "consumer_key"),  # PerConsumerRateLimiter
        ],
        ids=["auth-login-limiter", "per-consumer-rate-limiter"],
    )
    def test_concurrent_bursts_on_one_key_never_exceed_capacity(
        self, pg_dsn, table_name, key_column
    ) -> None:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        _isolated_table(pg_dsn, table_name, key_column)
        try:
            capacity = 10
            num_threads = 20
            attempts_per_thread = 5  # 100 total attempts >> capacity

            pools = [
                ConnectionPool(pg_dsn, min_size=1, max_size=2)
                for _ in range(num_threads)
            ]
            managers = [
                TokenBucketManager(
                    capacity=capacity,
                    refill_rate=0.0,  # isolates the pure decrement race
                    table_name=table_name,
                    key_column=key_column,
                )
                for _ in range(num_threads)
            ]
            for manager, pool in zip(managers, pools):
                manager.set_connection_pool(pool)

            results: list = []
            results_lock = threading.Lock()
            start_barrier = threading.Barrier(num_threads)

            def worker(manager: TokenBucketManager) -> None:
                start_barrier.wait()
                for _ in range(attempts_per_thread):
                    allowed, _ = manager.consume("shared-key")
                    with results_lock:
                        results.append(allowed)

            threads = [threading.Thread(target=worker, args=(m,)) for m in managers]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            for t in threads:
                assert not t.is_alive(), "worker thread did not complete in time"

            try:
                allowed_count = results.count(True)
                assert allowed_count == capacity, (
                    f"[{table_name}] Expected EXACTLY capacity={capacity} "
                    f"allowed across {num_threads} concurrent simulated "
                    f"nodes ({num_threads * attempts_per_thread} total "
                    f"attempts), got {allowed_count} -- overshoot detected "
                    f"(SELECT-then-UPDATE race window still open)."
                )
                assert len(results) == num_threads * attempts_per_thread
            finally:
                for pool in pools:
                    pool.close()
        finally:
            _drop_table(pg_dsn, table_name)


@pytest.mark.skipif(not HAS_PSYCOPG, reason="psycopg not available")
class TestPgConsumeRefillPreservation:
    """Proves the atomic path's inline SQL refill math (elapsed-time refill,
    capacity clamp) matches the original Python arithmetic exactly."""

    def test_refill_and_capacity_clamp_preserved_through_atomic_path(
        self, pg_dsn
    ) -> None:
        import psycopg
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        table_name = "token_bucket_state_refill_1334_test"
        key_column = "username"
        _isolated_table(pg_dsn, table_name, key_column)
        pool = ConnectionPool(pg_dsn, min_size=1, max_size=2)
        try:
            capacity = 5
            refill_rate = 2.0  # 2 tokens/sec
            manager = TokenBucketManager(
                capacity=capacity,
                refill_rate=refill_rate,
                table_name=table_name,
                key_column=key_column,
            )
            manager.set_connection_pool(pool)

            # Drain the bucket completely.
            for _ in range(capacity):
                allowed, _ = manager.consume("refill-key")
                assert allowed is True
            denied, retry = manager.consume("refill-key")
            assert denied is False
            assert retry > 0.0

            # Simulate 1 second elapsed by rewinding last_refill.
            with psycopg.connect(pg_dsn) as conn:
                conn.execute(
                    f"UPDATE {table_name} SET last_refill = last_refill - 1.0 "
                    f"WHERE {key_column} = %s",
                    ("refill-key",),
                )
                conn.commit()

            # 1s * 2 tokens/sec = 2 tokens refilled -> 2 allowed, 3rd denied.
            allowed1, _ = manager.consume("refill-key")
            allowed2, _ = manager.consume("refill-key")
            allowed3, _ = manager.consume("refill-key")
            assert allowed1 is True
            assert allowed2 is True
            assert allowed3 is False

            # Simulate a huge elapsed time -> must clamp at capacity, never
            # overshoot it.
            with psycopg.connect(pg_dsn) as conn:
                conn.execute(
                    f"UPDATE {table_name} "
                    "SET last_refill = last_refill - 1000000.0 "
                    f"WHERE {key_column} = %s",
                    ("refill-key",),
                )
                conn.commit()
            allowed_after_clamp = [
                manager.consume("refill-key")[0] for _ in range(capacity)
            ]
            assert allowed_after_clamp == [True] * capacity
            denied_after_clamp, _ = manager.consume("refill-key")
            assert denied_after_clamp is False
        finally:
            pool.close()
            _drop_table(pg_dsn, table_name)
