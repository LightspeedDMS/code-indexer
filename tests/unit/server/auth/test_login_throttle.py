"""Progressive per-username login throttle.

A login is THROTTLED after repeated failures; there is no lock state.
Each attempt is RESERVED (``begin_attempt``) before its password is
checked: while a backoff window runs the attempt is refused after a plain
read (no write lock); otherwise it is counted and admitted in one row-locked
transaction.  The attempt that reaches ``max_attempts`` starts the window;
each later admitted attempt doubles it, capped.  A passed check clears the
key (``record_success``).  The key is the username; windows always end
(cap).

The state is DB-backed (shared SQLite file in solo mode, PostgreSQL in a
cluster) so every worker/node sees it.  Time is controlled by an injected
clock, never by sleeping.  PostgreSQL cases skip unless TEST_POSTGRES_DSN
is set.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Tuple

import pytest

from code_indexer.server.auth.login_rate_limiter import (
    SCOPE_LOGIN,
    SCOPE_STEP_UP,
    AttemptOutcome,
    LoginRateLimiter,
    throttle_key,
)

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

# The shipped policy (no config setting exists for it).
_THRESHOLD = 5
_BASE_DELAY = 5.0
_CAP = 120.0
_WINDOW_SECONDS = 15 * 60.0
_BURST = 16
_JOIN_TIMEOUT_S = 120


class FakeClock:
    """A controllable wall clock (seconds since the epoch)."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Backend:
    def __init__(
        self,
        make: Callable[..., LoginRateLimiter],
        rows: Callable[[], List[Tuple[Any, ...]]],
    ) -> None:
        self.make = make
        self.rows = rows


def _sqlite_backend(tmp_path: Path) -> _Backend:
    db_path = str(tmp_path / "cidx_server.db")

    def make(**kwargs: Any) -> LoginRateLimiter:
        limiter = LoginRateLimiter(**kwargs)
        limiter.set_sqlite_path(db_path)
        return limiter

    def rows() -> List[Tuple[Any, ...]]:
        conn = sqlite3.connect(db_path)
        try:
            return list(conn.execute("SELECT * FROM login_throttle").fetchall())
        finally:
            conn.close()

    return _Backend(make, rows)


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """One throwaway, fully migrated database per module (dropped after)."""
    if not _DSN:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"throttle_{uuid.uuid4().hex[:12]}"
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


def _postgres_backend(dsn: str) -> Iterator[_Backend]:
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE login_throttle")
    pools: List[Any] = []

    def make(**kwargs: Any) -> LoginRateLimiter:
        pool = ConnectionPool(dsn, min_size=1, max_size=2)
        pools.append(pool)
        limiter = LoginRateLimiter(**kwargs)
        limiter.set_connection_pool(pool)
        return limiter

    def rows() -> List[Tuple[Any, ...]]:
        with psycopg.connect(dsn) as conn:
            return list(conn.execute("SELECT * FROM login_throttle").fetchall())

    try:
        yield _Backend(make, rows)
    finally:
        for pool in pools:
            pool.close()


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path: Path) -> Iterator[_Backend]:
    if request.param == "sqlite":
        yield _sqlite_backend(tmp_path)
    else:
        yield from _postgres_backend(request.getfixturevalue("pg_dsn"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _reach_threshold(limiter: LoginRateLimiter, subject: str) -> AttemptOutcome:
    outcome = AttemptOutcome(True, 0.0, False)
    for _ in range(_THRESHOLD):
        outcome = limiter.begin_attempt(subject)
    assert limiter.is_throttled(subject)[0] is True
    return outcome


class TestThreshold:
    def test_attempts_below_threshold_are_admitted_unthrottled(self, backend, clock):
        limiter = backend.make(clock=clock)
        for _ in range(_THRESHOLD - 1):
            assert limiter.begin_attempt("alice") == (True, 0.0, False)
        assert limiter.is_throttled("alice") == (False, 0.0)

    def test_threshold_attempt_is_admitted_and_starts_the_base_window(
        self, backend, clock
    ):
        limiter = backend.make(clock=clock)
        outcomes = [limiter.begin_attempt("alice") for _ in range(_THRESHOLD)]
        last = outcomes[-1]
        assert (last.admitted, last.throttle_started) == (True, True)
        assert last.retry_after_seconds == pytest.approx(_BASE_DELAY)
        assert [o.throttle_started for o in outcomes].count(True) == 1
        assert limiter.is_throttled("alice") == (True, pytest.approx(_BASE_DELAY))

    def test_window_elapses_without_any_unlock(self, backend, clock):
        limiter = backend.make(clock=clock)
        _reach_threshold(limiter, "alice")
        clock.advance(_BASE_DELAY - 1)
        assert limiter.begin_attempt("alice") == (False, pytest.approx(1.0), False)
        clock.advance(1)
        assert limiter.begin_attempt("alice").admitted is True

    def test_throttle_is_per_username(self, backend, clock):
        limiter = backend.make(clock=clock)
        _reach_threshold(limiter, "alice")
        assert limiter.begin_attempt("bob").admitted is True


class TestBackoff:
    def test_window_doubles_and_is_capped(self, backend, clock):
        limiter = backend.make(clock=clock)
        observed = [_reach_threshold(limiter, "alice").retry_after_seconds]
        for _ in range(7):
            clock.advance(observed[-1])
            outcome = limiter.begin_attempt("alice")
            assert outcome.admitted and not outcome.throttle_started
            observed.append(outcome.retry_after_seconds)
        assert observed == pytest.approx([5, 10, 20, 40, 80, 120, 120, 120])

    def test_cap_holds_after_many_attempts(self, backend, clock):
        # 40 attempts: past the threshold by more than the 32-step exponent
        # bound, so the doubling never overflows and the cap still holds.
        limiter = backend.make(clock=clock)
        for _ in range(40):
            clock.advance(_CAP)
            limiter.begin_attempt("admin")
        throttled, remaining = limiter.is_throttled("admin")
        assert throttled is True
        assert remaining <= _CAP
        clock.advance(_CAP)
        assert limiter.begin_attempt("admin").admitted is True

    def test_refused_attempt_is_not_counted(self, backend, clock):
        limiter = backend.make(clock=clock)
        _reach_threshold(limiter, "alice")
        refused = limiter.begin_attempt("alice")
        assert refused == (False, pytest.approx(_BASE_DELAY), False)
        clock.advance(_BASE_DELAY)
        after = limiter.begin_attempt("alice")
        assert after.retry_after_seconds == pytest.approx(2 * _BASE_DELAY)


class TestReset:
    def test_success_after_the_window_resets_the_counter(self, backend, clock):
        limiter = backend.make(clock=clock)
        _reach_threshold(limiter, "alice")
        clock.advance(_BASE_DELAY)
        assert limiter.begin_attempt("alice").admitted is True
        limiter.record_success("alice")
        for _ in range(_THRESHOLD - 1):
            assert limiter.begin_attempt("alice") == (True, 0.0, False)
        assert limiter.begin_attempt("alice").throttle_started is True

    def test_correct_attempt_reaching_the_threshold_is_cleared_by_success(
        self, backend, clock
    ):
        # The admitted attempt that reaches the threshold set a window; a
        # passed check deletes the row, so the user is not throttled.
        limiter = backend.make(clock=clock)
        _reach_threshold(limiter, "alice")
        limiter.record_success("alice")
        assert limiter.is_throttled("alice") == (False, 0.0)
        assert backend.rows() == []

    def test_attempts_older_than_the_window_are_forgotten(self, backend, clock):
        limiter = backend.make(clock=clock)
        for _ in range(_THRESHOLD - 1):
            limiter.begin_attempt("alice")
        clock.advance(_WINDOW_SECONDS + 1)
        assert limiter.begin_attempt("alice") == (True, 0.0, False)
        assert limiter.is_throttled("alice") == (False, 0.0)


class TestConcurrency:
    def test_concurrent_reservations_admit_exactly_the_threshold(
        self, backend, clock, monkeypatch
    ):
        # Several workers/nodes reserve for one key at the same instant:
        # the row lock serialises them, so exactly _THRESHOLD are admitted --
        # also under the shipped per-process rate cap (burst 10, frozen clock
        # so nothing refills; PostgreSQL takes no token).
        from code_indexer.server.auth import login_rate_limiter as module
        from code_indexer.server.auth.token_bucket import TokenBucket

        monkeypatch.setattr(
            module,
            "_RESERVATION_BUCKET",
            TokenBucket(
                capacity=module.RESERVATION_BURST,
                refill_rate=module.RESERVATIONS_PER_SECOND,
                time_fn=lambda: 0.0,
            ),
        )
        workers = [backend.make(clock=clock) for _ in range(4)]
        barrier = threading.Barrier(_BURST)
        outcomes: List[Optional[AttemptOutcome]] = []
        guard = threading.Lock()

        def reserve(limiter: LoginRateLimiter) -> None:
            barrier.wait(timeout=30)
            try:
                outcome: Optional[AttemptOutcome] = limiter.begin_attempt("admin")
            except module.ThrottleStoreBusy:
                outcome = None  # over the rate cap: refused as busy
            with guard:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=reserve, args=(workers[i % len(workers)],))
            for i in range(_BURST)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT_S)
        assert len(outcomes) == _BURST
        answered = [o for o in outcomes if o is not None]
        assert sum(o.admitted for o in answered) == _THRESHOLD
        assert sum(o.throttle_started for o in answered) == 1


class TestScopes:
    def test_scopes_never_share_a_key(self, backend, clock):
        limiter = backend.make(clock=clock)
        for _ in range(_THRESHOLD):
            limiter.begin_attempt("192.0.2.10:admin", scope=SCOPE_STEP_UP)
        assert limiter.is_throttled("192.0.2.10:admin", scope=SCOPE_STEP_UP)[0]
        assert limiter.is_throttled("192.0.2.10:admin") == (False, 0.0)
        assert limiter.begin_attempt("192.0.2.10:admin").admitted is True

    def test_no_subject_spells_another_scopes_key(self):
        assert throttle_key("a", SCOPE_LOGIN) != throttle_key("a", SCOPE_STEP_UP)
        assert throttle_key(f"{SCOPE_STEP_UP}\0a", SCOPE_LOGIN) != throttle_key(
            "a", SCOPE_STEP_UP
        )

    def test_lone_surrogate_username_is_throttled_not_an_error(self, backend, clock):
        # A JSON body can carry "\ud800"; it must be keyed, not crash.
        odd = "admin\ud800"
        assert len(throttle_key(odd)) == 64
        assert throttle_key(odd) != throttle_key("admin")
        limiter = backend.make(clock=clock)
        _reach_threshold(limiter, odd)
        assert limiter.begin_attempt(odd).admitted is False

    def test_unknown_scope_is_rejected(self):
        with pytest.raises(ValueError):
            throttle_key("alice", "other")


class TestStorage:
    def test_state_is_shared_between_workers(self, backend, clock):
        worker_a = backend.make(clock=clock)
        worker_b = backend.make(clock=clock)
        for _ in range(_THRESHOLD - 1):
            worker_a.begin_attempt("alice")
        assert worker_b.begin_attempt("alice").throttle_started is True
        assert worker_a.is_throttled("alice")[0] is True
        worker_b.record_success("alice")
        assert worker_a.is_throttled("alice") == (False, 0.0)

    def test_one_row_per_username_and_expired_rows_are_pruned(self, backend, clock):
        limiter = backend.make(clock=clock)
        for i in range(30):
            limiter.begin_attempt(f"random-{i}")
        limiter.begin_attempt("random-0")
        assert len(backend.rows()) == 30
        clock.advance(_WINDOW_SECONDS + 1)
        limiter.begin_attempt("late")
        assert len(backend.rows()) == 1

    def test_only_new_rows_prune(self, backend, clock):
        limiter = backend.make(clock=clock)
        for i in range(5):
            limiter.begin_attempt(f"old-{i}")
        limiter.begin_attempt("regular")
        clock.advance(_WINDOW_SECONDS + 1)
        # An existing key's attempt updates its row: no pruning work.
        limiter.begin_attempt("regular")
        assert len(backend.rows()) == 6
        # A new key inserts a row and prunes the expired ones first.
        limiter.begin_attempt("newcomer")
        assert len(backend.rows()) == 2

    def test_success_deletes_the_row(self, backend, clock):
        limiter = backend.make(clock=clock)
        limiter.begin_attempt("alice")
        limiter.record_success("alice")
        assert backend.rows() == []

    def test_username_is_never_stored_in_clear(self, backend, clock):
        limiter = backend.make(clock=clock)
        typed = "Tr0ub4dor&3-typed-as-username"
        limiter.begin_attempt(typed)
        assert typed not in repr(backend.rows())


class TestSqliteWriteLock:
    """Solo mode shares cidx_server.db with every other server store, so a
    refused attempt must not take (or wait for) its write lock."""

    _HOLD_S = 3.0  # how long a competing writer holds BEGIN IMMEDIATE
    _REFUSAL_BUDGET_S = 0.5

    def test_refusal_takes_no_write_lock(self, tmp_path: Path, clock):
        import time

        db_path = str(tmp_path / "cidx_server.db")
        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(db_path)
        _reach_threshold(limiter, "alice")

        writer = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        writer.execute("BEGIN IMMEDIATE")
        release = threading.Timer(self._HOLD_S, lambda: writer.execute("COMMIT"))
        release.start()
        try:
            started = time.monotonic()
            outcome = limiter.begin_attempt("alice")
            elapsed = time.monotonic() - started
        finally:
            release.join()
            writer.close()

        assert outcome.admitted is False
        assert elapsed < self._REFUSAL_BUDGET_S, elapsed

    def test_reservations_reuse_the_shared_connection(
        self, tmp_path: Path, clock, monkeypatch
    ):
        # The store keeps its OWN connection per thread (opened once, never
        # per call, never the shared manager connection).
        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(str(tmp_path / "cidx_server.db"))
        limiter.begin_attempt("warm-up")

        opened: List[Any] = []
        real_connect = sqlite3.connect

        def counting_connect(*args: Any, **kwargs: Any) -> Any:
            opened.append(args)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        for i in range(10):
            limiter.begin_attempt(f"user-{i}")
            limiter.record_success(f"user-{i}")
        assert opened == []

    # _HOLD_S and _REFUSAL_BUDGET_S are defined at the top of this class.
    _HOLD_LONG_S = 8.0  # a writer that outlasts the throttle's bound
    _BUSY_BOUND_S = 2.0  # the reservation's SQLite busy timeout
    _LOCK_HANDOFF_WAIT_S = 5.0  # wait for the helper thread to take a lock

    def test_admitted_attempt_waits_at_most_the_bound(self, tmp_path: Path, clock):
        import time

        db_path = str(tmp_path / "cidx_server.db")
        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(db_path)
        writer = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        writer.execute("BEGIN IMMEDIATE")
        release = threading.Timer(self._HOLD_LONG_S, lambda: writer.execute("COMMIT"))
        release.start()
        raised = None
        try:
            started = time.monotonic()
            try:
                limiter.begin_attempt("bob")
            except Exception as exc:  # the store's busy error
                raised = exc
            elapsed = time.monotonic() - started
        finally:
            release.cancel()
            if writer.in_transaction:
                writer.execute("COMMIT")
            writer.close()

        # The worker gives up after the bound instead of waiting out the
        # writer (the door then answers 503, "try again shortly").
        assert type(raised).__name__ == "ThrottleStoreBusy", (raised, elapsed)
        assert elapsed < self._BUSY_BOUND_S + 1.0, elapsed

    def test_refusal_does_not_wait_on_the_manager_lock(self, tmp_path: Path, clock):
        import time

        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = str(tmp_path / "cidx_server.db")
        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(db_path)
        _reach_threshold(limiter, "alice")

        manager = DatabaseConnectionManager.get_instance(db_path)
        held, done = threading.Event(), threading.Event()

        def hold_manager_lock() -> None:
            with manager._lock:
                held.set()
                done.wait(timeout=self._HOLD_S)

        holder = threading.Thread(target=hold_manager_lock)
        holder.start()
        assert held.wait(timeout=self._LOCK_HANDOFF_WAIT_S)
        try:
            started = time.monotonic()
            outcome = limiter.begin_attempt("alice")
            elapsed = time.monotonic() - started
        finally:
            done.set()
            holder.join()

        assert outcome.admitted is False
        assert elapsed < self._REFUSAL_BUDGET_S, elapsed

    # The shipped per-process reservation rate cap (tokens per second).
    _BUCKET_RATE = 10.0

    def _install_bucket(self, monkeypatch, capacity: int, tick: FakeClock) -> None:
        """Pin the per-process reservation bucket to a fake monotonic clock
        (the tree-wide conftest installs an unlimited one for other tests)."""
        from code_indexer.server.auth import login_rate_limiter as module
        from code_indexer.server.auth.token_bucket import TokenBucket

        bucket = TokenBucket(
            capacity=capacity, refill_rate=self._BUCKET_RATE, time_fn=tick
        )
        monkeypatch.setattr(module, "_RESERVATION_BUCKET", bucket, raising=False)

    def test_reservation_rate_is_capped_per_process(
        self, tmp_path: Path, clock, monkeypatch
    ):
        from code_indexer.server.auth import login_rate_limiter as module

        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(str(tmp_path / "cidx_server.db"))
        tick = FakeClock(start=0.0)
        self._install_bucket(monkeypatch, capacity=10, tick=tick)

        # Burst of 10 new reservations, then the 11th is refused as busy at
        # once (no waiting for a token).
        for i in range(10):
            assert limiter.begin_attempt(f"user-{i}").admitted is True
        with pytest.raises(module.ThrottleStoreBusy):
            limiter.begin_attempt("user-10")
        # 0.1 s later one token has refilled: exactly one more.
        tick.advance(0.1)
        assert limiter.begin_attempt("user-10").admitted is True
        with pytest.raises(module.ThrottleStoreBusy):
            limiter.begin_attempt("user-11")
        assert (module.RESERVATIONS_PER_SECOND, module.RESERVATION_BURST) == (
            10.0,
            10,
        )

    def test_refusals_and_success_deletes_take_no_token(
        self, tmp_path: Path, clock, monkeypatch
    ):
        from code_indexer.server.auth import login_rate_limiter as module

        db_path = str(tmp_path / "cidx_server.db")
        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(db_path)
        _reach_threshold(limiter, "alice")
        limiter.begin_attempt("bob")
        self._install_bucket(monkeypatch, capacity=1, tick=FakeClock(start=0.0))

        for _ in range(20):
            assert limiter.begin_attempt("alice").admitted is False
        limiter.record_success("bob")
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM login_throttle").fetchone()[0]
        finally:
            conn.close()
        assert rows == 1  # bob's row deleted without a token; alice's kept
        # The single token is still there for one new reservation, no more.
        assert limiter.begin_attempt("newcomer").admitted is True
        with pytest.raises(module.ThrottleStoreBusy):
            limiter.begin_attempt("another")

    def test_co_tenant_transaction_is_unaffected(self, tmp_path: Path, clock):
        from code_indexer.server.auth.login_rate_limiter import ThrottleStoreBusy
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = str(tmp_path / "cidx_server.db")
        limiter = LoginRateLimiter(clock=clock)
        limiter.set_sqlite_path(db_path)
        shared = DatabaseConnectionManager.get_instance(db_path).get_connection()
        shared.execute("CREATE TABLE IF NOT EXISTS co_tenant (v TEXT)")
        shared.commit()
        # Another store on this thread leaves an implicit write transaction
        # open on the shared connection.
        shared.execute("INSERT INTO co_tenant VALUES ('half-finished')")
        assert shared.in_transaction
        try:
            # The throttle has its own connection: it cannot write while the
            # co-tenant holds the file's write lock, so it gives up after the
            # bound (the door answers 503) -- it neither breaks nor commits
            # the co-tenant's transaction.
            with pytest.raises(ThrottleStoreBusy):
                limiter.begin_attempt("carol")
            limiter.record_success("carol")  # kept row, logged, no raise
            assert shared.in_transaction
        finally:
            shared.rollback()
        rows = shared.execute("SELECT COUNT(*) FROM co_tenant").fetchone()[0]
        assert rows == 0  # the throttle never committed the co-tenant's row
        assert limiter.begin_attempt("carol").admitted is True


class TestConfiguration:
    def test_cap_must_be_shorter_than_the_attempt_window(self):
        with pytest.raises(ValueError):
            LoginRateLimiter(max_delay_seconds=_WINDOW_SECONDS)

    def test_disabled_limiter_never_throttles(self, backend, clock):
        limiter = backend.make(clock=clock, enabled=False)
        for _ in range(3 * _THRESHOLD):
            assert limiter.begin_attempt("alice") == (True, 0.0, False)
        assert limiter.is_throttled("alice") == (False, 0.0)
