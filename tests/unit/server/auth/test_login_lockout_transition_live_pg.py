<<<<<<< HEAD
"""Live PostgreSQL: a lockout transition is reported by exactly one node.

Several LoginRateLimiter instances (one per simulated node, each with its
own connection pool) record the failure that crosses the threshold at the
same moment.  The lockout upsert is conditional, so exactly one of them
reports ``lockout_started`` -- the login door records one lockout row per
lockout across the cluster.  Skipped unless TEST_POSTGRES_DSN is set.
=======
"""Live PostgreSQL: concurrent reservations across nodes cannot pass the
throttle, and the throttle start is reported by exactly one node.

Several LoginRateLimiter instances (one per simulated node, each with its
own connection pool) reserve the attempt that reaches the threshold at the
same moment.  The throttle row is locked for each reservation, so exactly
one of them is admitted and reports ``throttle_started`` -- the login doors
record one ``rate_limited`` row per throttle across the cluster -- and the
others are refused before any password check.  Skipped unless
TEST_POSTGRES_DSN is set.
>>>>>>> upstream/master
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Iterator, List

import pytest

from code_indexer.server.auth.login_rate_limiter import (
<<<<<<< HEAD
    FailureOutcome,
=======
    AttemptOutcome,
>>>>>>> upstream/master
    LoginRateLimiter,
)

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="No PostgreSQL available (set TEST_POSTGRES_DSN to enable)"
)

_NODES = 8
_MAX_ATTEMPTS = 3


@pytest.fixture()
def fresh_dsn() -> Iterator[str]:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"lockout_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(_DSN)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        yield dsn
    finally:
        with psycopg.connect(_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


<<<<<<< HEAD
def test_concurrent_threshold_failures_report_one_transition(fresh_dsn: str) -> None:
=======
def test_concurrent_threshold_reservations_admit_one(fresh_dsn: str) -> None:
>>>>>>> upstream/master
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pools = [ConnectionPool(fresh_dsn, min_size=1, max_size=2) for _ in range(_NODES)]
    try:
        nodes = []
        for pool in pools:
            limiter = LoginRateLimiter(max_attempts=_MAX_ATTEMPTS)
            limiter.set_connection_pool(pool)
            nodes.append(limiter)
        for _ in range(_MAX_ATTEMPTS - 1):
<<<<<<< HEAD
            assert nodes[0].record_failure("grace").lockout_started is False

        # Every node passed its lock pre-check; all record the crossing
        # failure at once (the unguarded write path).
        barrier = threading.Barrier(_NODES)
        outcomes: List[FailureOutcome] = []
        guard = threading.Lock()

        def _fail(node: LoginRateLimiter) -> None:
            barrier.wait(timeout=30)
            outcome = node._pg_record_failure("grace")
            with guard:
                outcomes.append(outcome)

        threads = [threading.Thread(target=_fail, args=(n,)) for n in nodes]
=======
            assert nodes[0].begin_attempt("grace").throttle_started is False

        # Every node reserves the threshold attempt at once.
        barrier = threading.Barrier(_NODES)
        outcomes: List[AttemptOutcome] = []
        guard = threading.Lock()

        def _reserve(node: LoginRateLimiter) -> None:
            barrier.wait(timeout=30)
            outcome = node.begin_attempt("grace")
            with guard:
                outcomes.append(outcome)

        threads = [threading.Thread(target=_reserve, args=(n,)) for n in nodes]
>>>>>>> upstream/master
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert len(outcomes) == _NODES
<<<<<<< HEAD
        assert all(o.locked for o in outcomes)
        assert sum(o.lockout_started for o in outcomes) == 1

        # Failures recorded while locked report no further transition.
        assert nodes[1].record_failure("grace").lockout_started is False
        assert nodes[2].is_locked("grace")[0] is True
=======
        assert sum(o.admitted for o in outcomes) == 1
        assert sum(o.throttle_started for o in outcomes) == 1

        # Reservations during the window are refused and report no start.
        assert nodes[1].begin_attempt("grace").admitted is False
        assert nodes[2].is_throttled("grace")[0] is True
>>>>>>> upstream/master
    finally:
        for pool in pools:
            pool.close()
