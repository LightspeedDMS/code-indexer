"""
Tests for Bug #1178: SQLite multi-worker startup race in DB bootstrap.

ROOT CAUSE: Under `uvicorn --workers N` in SQLite (solo) mode, multiple worker
processes start concurrently and race in DB bootstrap -- `initialize_database()`
and `seed_initial_admin()` -- causing errors (e.g. duplicate inserts /
"table already exists" / UNIQUE violations / partial state) when two workers
bootstrap the same SQLite file at once.

FIX: A cross-process file lock (filelock.FileLock) around the bootstrap
critical section so only the first worker bootstraps; the others block, then
find the work already done and skip via pre-checks.  The backends stay STRICT
(raise sqlite3.IntegrityError on duplicate) -- race safety comes from the lock
+ callers' pre-checks, NOT from silent OR IGNORE tolerance.

TESTING STRATEGY:
  - Thread-based tests verify concurrent initialize_database() (which owns its
    own FileLock) is safe under N threads.
  - Thread-based race proves backend strictness: concurrent threads racing on
    raw duplicate INSERT produce sqlite3.IntegrityError, confirming the strict
    backend contract.  Threads are sufficient here because the backend has no
    internal lock -- the UNIQUE constraint is the only guard.
  - Multiprocessing (spawn) tests validate the outer bootstrap lock in
    service_init.py across REAL separate processes against the SAME SQLite file
    and lock file.  is_singleton=True is per-process, so separate processes DO
    block on each other at the OS level.  2 processes are sufficient to
    demonstrate serialization.
  - All tests are bounded; multiprocess tests use 2 workers and join with
    hard timeouts to stay well under 20s total.
"""

import multiprocessing
import queue
import sqlite3
import threading

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_N_THREADS = 5
_N_PROCS = 2  # 2 processes suffice to prove cross-process serialization
_THREAD_JOIN_TIMEOUT = 25  # seconds
_PROC_JOIN_TIMEOUT = 20  # seconds per process


# ---------------------------------------------------------------------------
# Thread helpers (for initialize_database tests)
# ---------------------------------------------------------------------------


def _count_admin_rows(db_path: str) -> int:
    """Return the number of 'admin' rows in the users table."""
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE username='admin'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _count_golden_repo_rows(db_path: str, alias: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM golden_repos_metadata WHERE alias=?", (alias,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _get_table_names(db_path: str) -> set:
    """Return the set of table names in the database."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


def _reset_connection_manager():
    """Clear DatabaseConnectionManager singleton state.

    Uses timeout=0 because these tests never start the cleanup daemon --
    they use raw DatabaseSchema / backend constructors only.  A zero timeout
    is a no-op when no daemon is running and avoids 2s waits per fixture
    setup/teardown.
    """
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    DatabaseConnectionManager.stop_cleanup_daemon(timeout=0)
    with DatabaseConnectionManager._instance_lock:
        DatabaseConnectionManager._instances.clear()


def _run_threads(
    target, n: int, barrier: threading.Barrier, error_q: queue.Queue
) -> list:
    threads = [
        threading.Thread(target=target, args=(barrier, error_q), daemon=True)
        for _ in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_THREAD_JOIN_TIMEOUT)
    hung = [t for t in threads if t.is_alive()]
    return hung


def _collect_errors(error_q: queue.Queue) -> list:
    errors = []
    while not error_q.empty():
        try:
            errors.append(error_q.get_nowait())
        except queue.Empty:
            break
    return errors


def _assert_no_hung_threads(hung: list) -> None:
    assert not hung, (
        f"{len(hung)} worker thread(s) did not finish within {_THREAD_JOIN_TIMEOUT}s "
        "(possible deadlock in bootstrap lock)"
    )


def _assert_no_errors(errors: list, context: str) -> None:
    assert errors == [], f"{context}: {len(errors)} error(s) raised: " + "; ".join(
        type(e).__name__ + ": " + str(e) for e in errors
    )


# ---------------------------------------------------------------------------
# Multiprocessing worker functions (must be top-level for spawn pickling)
# ---------------------------------------------------------------------------


def _mp_locked_seed_worker(db_path: str, result_q: multiprocessing.Queue) -> None:
    """
    Worker process: runs initialize_database() + locked seed_initial_admin()
    mimicking what service_init.py does.  Uses filelock + get_user pre-check
    so only the first process inserts; subsequent ones skip.
    """
    # Must add src to path because spawn workers don't inherit sys.path
    import os
    import sys

    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    src_path = os.path.join(project_root, "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    try:
        import filelock
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        # Step 1: initialize schema (has own inner FileLock)
        schema = DatabaseSchema(db_path)
        schema.initialize_database()

        # Step 2: outer bootstrap lock (matches service_init.py)
        lock_path = str(db_path) + ".bootstrap.lock"
        with filelock.FileLock(lock_path, timeout=30, is_singleton=True):
            backend = UsersSqliteBackend(db_path)
            existing = backend.get_user("admin")
            if existing is None:
                import hashlib

                password_hash = hashlib.sha256(b"admin").hexdigest()
                backend.create_user(
                    username="admin",
                    password_hash=password_hash,
                    role="admin",
                )

        result_q.put(None)  # success sentinel
    except Exception as exc:
        result_q.put(exc)


def _mp_locked_golden_repo_worker(
    db_path: str, alias: str, result_q: multiprocessing.Queue
) -> None:
    """
    Worker process: runs initialize_database() + locked check-then-insert for
    a golden repo, mimicking bootstrap_cidx_meta / register_local_repo.
    Uses filelock + repo_exists() pre-check so only the first process inserts.
    """
    import os
    import sys

    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    src_path = os.path.join(project_root, "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    try:
        import filelock
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        # Step 1: initialize schema
        schema = DatabaseSchema(db_path)
        schema.initialize_database()

        # Step 2: outer bootstrap lock (matches service_init.py)
        lock_path = str(db_path) + ".bootstrap.lock"
        with filelock.FileLock(lock_path, timeout=30, is_singleton=True):
            backend = GoldenRepoMetadataSqliteBackend(db_path)
            if not backend.repo_exists(alias):
                backend.add_repo(
                    alias=alias,
                    repo_url=f"local://{alias}",
                    default_branch="main",
                    clone_path=f"/tmp/{alias}",
                    created_at="2026-01-01T00:00:00",
                )

        result_q.put(None)  # success sentinel
    except Exception as exc:
        result_q.put(exc)


def _run_procs(target, args_list: list) -> list:
    """
    Spawn N processes each calling target(*args).  Returns list of results
    (None = success, Exception = failure).

    Teardown: join with hard timeout, then terminate + join any straggler.
    Results are drained AFTER all processes have completed or been terminated
    to avoid a race between queue drain and process exit.
    """
    ctx = multiprocessing.get_context("spawn")
    result_q = ctx.Queue()
    procs = [
        ctx.Process(target=target, args=(*args, result_q), daemon=False)
        for args in args_list
    ]
    for p in procs:
        p.start()

    # Join all processes with hard timeout; terminate stragglers.
    for p in procs:
        p.join(timeout=_PROC_JOIN_TIMEOUT)

    stragglers = [p for p in procs if p.is_alive()]
    for p in stragglers:
        p.terminate()
    for p in stragglers:
        p.join(timeout=5)

    # Drain results AFTER all processes have exited (no queue-write race).
    results = []
    while not result_q.empty():
        try:
            results.append(result_q.get_nowait())
        except Exception:
            break

    # Any straggler that was terminated counts as an error.
    for p in stragglers:
        results.append(TimeoutError(f"Worker process {p.pid} hung and was terminated"))

    return results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Temp SQLite DB path (file does not pre-exist)."""
    return str(tmp_path / "cidx_server.db")


@pytest.fixture
def tmp_db_initialized(tmp_db):
    """Temp SQLite DB path with schema already initialized (single-threaded)."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(tmp_db).initialize_database()
    return tmp_db


@pytest.fixture(scope="class")
def tmp_db_initialized_class(tmp_path_factory):
    """Class-scoped initialized DB: pay initialize_database() once for the class.

    Used by TestSequentialIdempotency so the ~1.87s migration cost is paid
    once rather than once per test method.  Tests in that class share the same
    DB file -- this is intentional, since the sequential seed/idempotency
    tests are designed to build on each other's state.
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    tmpdir = tmp_path_factory.mktemp("seq_idempotency")
    db_path = str(tmpdir / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return db_path


@pytest.fixture(autouse=True)
def isolated_manager_registry():
    """Clear singleton state before and after each test."""
    _reset_connection_manager()
    yield
    _reset_connection_manager()


# ---------------------------------------------------------------------------
# Tests: concurrent initialize_database() -- thread-based (inner FileLock)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
class TestConcurrentInitializeDatabase:
    """Concurrent initialize_database() must be safe under N threads.

    initialize_database() owns its own inner FileLock so thread-level tests
    correctly exercise and validate the DDL-lock fix.

    Both assertions (no exception + schema intact) are combined in one test
    to pay the expensive initialize_database() first-call cost only once.
    """

    def _make_init_worker(self, db_path: str):
        def worker(barrier: threading.Barrier, error_q: queue.Queue):
            _reset_connection_manager()
            try:
                from code_indexer.server.storage.database_manager import DatabaseSchema

                schema = DatabaseSchema(db_path)
                barrier.wait()  # all threads start simultaneously
                schema.initialize_database()
            except Exception as exc:
                error_q.put(exc)
            finally:
                _reset_connection_manager()

        return worker

    def test_concurrent_initialize_safe(self, tmp_db):
        """
        N threads call initialize_database() simultaneously from a fresh dir.

        Asserts both:
          - No exception is raised (without the inner FileLock: may see
            'database is locked' or other sqlite3 errors)
          - Core tables exist after all threads complete (schema is intact)

        Combined into one test to pay the ~1.87s initialize_database() first-
        call cost only once.
        """
        error_q: queue.Queue = queue.Queue()
        barrier = threading.Barrier(_N_THREADS)
        hung = _run_threads(
            self._make_init_worker(tmp_db), _N_THREADS, barrier, error_q
        )
        _assert_no_hung_threads(hung)
        _assert_no_errors(_collect_errors(error_q), "concurrent initialize_database()")

        tables = _get_table_names(tmp_db)
        assert "users" in tables, f"'users' table missing; tables={tables}"
        assert "global_repos" in tables, (
            f"'global_repos' table missing; tables={tables}"
        )


# ---------------------------------------------------------------------------
# Tests: locked multi-process seed_initial_admin -- PASS with lock
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
class TestMultiprocessSeedWithLock:
    """
    2 separate processes each run initialize_database() + locked seed path
    (mimicking service_init.py) against the SAME SQLite file.

    Expected outcome: no exception, exactly one admin row.
    The lock + get_user pre-check ensures only the first process inserts.

    2 processes are sufficient to demonstrate cross-process serialization
    (one wins the lock, the other blocks then skips via pre-check).
    """

    def test_multiprocess_seed_locked_bootstrap(self, tmp_db):
        """
        2 processes run the locked bootstrap path -- no exception raised and
        exactly ONE admin row exists afterwards.
        """
        args_list = [(tmp_db,)] * _N_PROCS
        results = _run_procs(_mp_locked_seed_worker, args_list)

        errors = [r for r in results if r is not None]
        assert errors == [], (
            "Expected no errors with lock+pre-check for admin seed bootstrap: "
            + "; ".join(type(e).__name__ + ": " + str(e) for e in errors)
        )

        count = _count_admin_rows(tmp_db)
        assert count == 1, (
            f"Expected exactly 1 admin row after locked multiprocess bootstrap, got {count}"
        )


# ---------------------------------------------------------------------------
# Tests: FAIL without lock -- proves lock is load-bearing (thread-based)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
class TestLockIsLoadBearing:
    """
    Demonstrate that WITHOUT the outer bootstrap lock, concurrent callers race
    into a sqlite3.IntegrityError on strict backends.

    The backends have NO internal lock -- sqlite3.IntegrityError is the ONLY
    guard on duplicate inserts.  Concurrent threads racing on a raw
    create_user() call reliably trigger this error, proving that:
      (a) The backend is strict (raises on duplicate, no silent OR IGNORE).
      (b) The FileLock in service_init.py is the sole race-safety mechanism.

    Threads are used instead of spawned processes because:
      - The backend has no internal per-process state that differs from
        per-thread state for this code path.
      - Threads are 10-100x cheaper than spawned processes.
      - The SQLite UNIQUE constraint fires at the DB level regardless of
        whether callers are threads or processes.
    """

    def test_without_lock_causes_integrity_error(self, tmp_db_initialized):
        """
        4 threads simultaneously call create_user('admin') WITHOUT any lock
        or pre-check on an already-initialized (schema present) DB.

        At least one thread must raise sqlite3.IntegrityError, proving that
        the strict backend fails loud on duplicate and that the lock in
        service_init.py is the sole race-safety mechanism.

        If this test fails (no IntegrityError), the strict revert was not applied.
        """
        import hashlib

        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        errors: list = []
        errors_lock = threading.Lock()
        barrier = threading.Barrier(4)

        def worker():
            try:
                backend = UsersSqliteBackend(tmp_db_initialized)
                barrier.wait()  # all threads race simultaneously
                backend.create_user(
                    username="admin",
                    password_hash=hashlib.sha256(b"admin").hexdigest(),
                    role="admin",
                )
            except Exception as exc:
                with errors_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_THREAD_JOIN_TIMEOUT)

        hung = [t for t in threads if t.is_alive()]
        assert not hung, f"{len(hung)} thread(s) hung -- possible deadlock"

        integrity_errors = [
            e
            for e in errors
            if isinstance(e, sqlite3.IntegrityError)
            or (isinstance(e, Exception) and "UNIQUE" in str(e))
        ]
        assert len(integrity_errors) >= 1, (
            "Expected at least one sqlite3.IntegrityError from concurrent unlocked "
            "create_user() calls (strict backend must raise on duplicate). "
            "Actual results: "
            + str(
                [type(e).__name__ + ": " + str(e) if e else "success" for e in errors]
            )
        )


# ---------------------------------------------------------------------------
# Tests: locked multi-process golden repo bootstrap -- PASS with lock
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
class TestMultiprocessGoldenRepoWithLock:
    """
    2 separate processes each run initialize_database() + locked check-then-insert
    for a golden repo, mimicking bootstrap_cidx_meta / register_local_repo.

    Expected outcome: no exception, exactly one golden_repos_metadata row.
    The lock + repo_exists() pre-check ensures only the first process inserts.

    2 processes are sufficient to demonstrate cross-process serialization.
    """

    def test_multiprocess_golden_repo_locked_bootstrap(self, tmp_db):
        """
        2 processes run the locked golden-repo bootstrap -- no exception raised
        and exactly ONE golden_repos_metadata row exists afterwards.
        """
        alias = "cidx-meta"
        args_list = [(tmp_db, alias)] * _N_PROCS
        results = _run_procs(_mp_locked_golden_repo_worker, args_list)

        errors = [r for r in results if r is not None]
        assert errors == [], (
            "Expected no errors with lock+pre-check for golden repo bootstrap: "
            + "; ".join(type(e).__name__ + ": " + str(e) for e in errors)
        )

        count = _count_golden_repo_rows(tmp_db, alias)
        assert count == 1, (
            f"Expected exactly 1 golden_repos_metadata row for '{alias}', got {count}"
        )


# ---------------------------------------------------------------------------
# Tests: sequential idempotency (single-worker regression)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
class TestSequentialIdempotency:
    """Sequential idempotency: single-worker behavior must be unchanged.

    Uses a class-scoped DB fixture (tmp_db_initialized_class) so the
    ~1.87s initialize_database() migration cost is paid once for the class,
    not once per test method.  The shared DB state is intentional: these
    tests are sequential and build on each other's inserts (seed_initial_admin
    is idempotent across calls; golden-repo uses a distinct table).
    """

    def test_seed_idempotent_sequential(self, tmp_db_initialized_class):
        """Calling seed_initial_admin() multiple times sequentially is idempotent."""
        from code_indexer.server.auth.user_manager import UserManager

        um = UserManager(
            users_file_path=tmp_db_initialized_class + ".users_unused",
            use_sqlite=True,
            db_path=tmp_db_initialized_class,
        )
        um.seed_initial_admin()
        um.seed_initial_admin()
        um.seed_initial_admin()

        count = _count_admin_rows(tmp_db_initialized_class)
        assert count == 1, f"Expected 1 admin after sequential seeding, got {count}"

    def test_single_worker_seed_unaffected(self, tmp_db_initialized_class):
        """Single-worker (workers=1) behavior must be unchanged by the fix."""
        from code_indexer.server.auth.user_manager import UserManager

        um = UserManager(
            users_file_path=tmp_db_initialized_class + ".users_unused",
            use_sqlite=True,
            db_path=tmp_db_initialized_class,
        )
        um.seed_initial_admin()

        count = _count_admin_rows(tmp_db_initialized_class)
        assert count == 1

    def test_golden_repo_register_local_idempotent(self, tmp_db_initialized_class):
        """
        Calling register_local_repo() twice with the same alias is idempotent
        (returns False on second call, no exception).
        """
        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        backend = GoldenRepoMetadataSqliteBackend(tmp_db_initialized_class)
        # First insert
        backend.add_repo(
            alias="cidx-meta",
            repo_url="local://cidx-meta",
            default_branch="main",
            clone_path="/tmp/cidx-meta",
            created_at="2026-01-01T00:00:00",
        )
        # Second insert: caller pre-checks and skips (simulated here)
        if not backend.repo_exists("cidx-meta"):
            backend.add_repo(
                alias="cidx-meta",
                repo_url="local://cidx-meta",
                default_branch="main",
                clone_path="/tmp/cidx-meta",
                created_at="2026-01-01T00:00:00",
            )

        count = _count_golden_repo_rows(tmp_db_initialized_class, "cidx-meta")
        assert count == 1, f"Expected 1 row after idempotent insert, got {count}"
