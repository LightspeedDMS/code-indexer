"""
Tests for Bug #1065: submit_job atomic dedup via register_job_if_no_conflict.

Bug description: submit_job used a TOCTOU two-step (in-process lock precheck +
non-atomic register_job) instead of the cluster-atomic register_job_if_no_conflict.
Worse, both the manager's persist insert and the register_job call swallowed
exceptions, so even a DB unique-constraint rejection did NOT stop the worker thread.

The bug manifests in the following scenario:
  1. Two nodes call submit_job simultaneously for the same (op_type, repo_alias).
  2. Both pass the in-process TOCTOU check (different process memories).
  3. Only the first DB-level INSERT can honour idx_active_job_per_repo.
  4. The second would get an IntegrityError — but register_job swallows it and
     starts the worker thread anyway. So BOTH workers run. That's the bug.

After the fix:
  - submit_job uses register_job_if_no_conflict (atomic INSERT) for repo-scoped ops.
  - A DuplicateJobError from the atomic gate propagates BEFORE the thread spawns.
  - No swallowing in the submit path.

Anti-mock rule: real SQLite DB with the real schema (including idx_active_job_per_repo).
"""

import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Dict, Any

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    DuplicateJobError,
)
from code_indexer.server.services.job_tracker import JobTracker


# ---------------------------------------------------------------------------
# DB schema helper — mirrors the schema from test_job_tracker_atomic_register.py
# ---------------------------------------------------------------------------


def _create_schema(db_path: str) -> None:
    """Create background_jobs table and idx_active_job_per_repo unique index."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            current_phase TEXT,
            phase_detail TEXT,
            actor_username TEXT,
            progress_info TEXT,
            metadata TEXT,
            executing_node TEXT,
            claimed_at TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()


def _make_db(tmp_path: Path) -> str:
    """Create a real SQLite DB with the full background_jobs schema."""
    db_path = str(tmp_path / "test_jobs.db")
    _create_schema(db_path)
    return db_path


def _make_manager(db_path: str) -> BackgroundJobManager:
    """
    Create a BackgroundJobManager wired to a real JobTracker on the same DB.
    No sqlite_backend — the tracker does the DB work directly.
    """
    tracker = JobTracker(db_path=db_path)
    manager = BackgroundJobManager(storage_path=None)
    manager._job_tracker = tracker  # type: ignore[assignment]
    return manager


def simple_job() -> Dict[str, Any]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Test class: atomic dedup for repo-scoped submit_job
# ---------------------------------------------------------------------------


class TestSubmitJobAtomicDedup:
    """
    Bug #1065: submit_job must use register_job_if_no_conflict for repo-scoped ops.
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = _make_db(Path(self.tmp))
        self.manager = _make_manager(self.db_path)

    def teardown_method(self):
        try:
            self.manager.shutdown()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    # -----------------------------------------------------------------------
    # AC1: first repo-scoped submit succeeds and job is persisted to DB
    # -----------------------------------------------------------------------
    def test_first_submit_returns_job_id(self):
        """A repo-scoped submit_job must return a valid job_id."""
        job_id = self.manager.submit_job(
            "global_repo_refresh",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="my-repo-global",
        )
        assert isinstance(job_id, str) and len(job_id) > 0

    def test_first_submit_job_is_in_db(self):
        """After atomic claim, the job row must exist in the DB immediately."""
        job_id = self.manager.submit_job(
            "global_repo_refresh",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="my-repo-global",
        )
        # The atomic claim must persist BEFORE the worker starts.
        # No sleep needed — the row is inserted synchronously in submit_job.
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT job_id, repo_alias, operation_type FROM background_jobs "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None, (
            f"Job {job_id} not found in DB immediately after submit_job — "
            "atomic claim was not persisted before thread spawn"
        )
        assert row[1] == "my-repo-global"
        assert row[2] == "global_repo_refresh"

    # -----------------------------------------------------------------------
    # AC2: duplicate raises DuplicateJobError BEFORE the worker thread starts
    # -----------------------------------------------------------------------
    def test_duplicate_repo_scoped_submit_raises_before_thread_spawns(self):
        """
        Regression for Bug #1065 core defect: the second submit for the same
        (operation_type, repo_alias) MUST raise DuplicateJobError and MUST NOT
        spawn a worker thread.
        """
        gate = threading.Event()  # holds the first worker until we release
        worker_started = threading.Event()  # signals the first worker is running
        thread_count = [0]
        count_lock = threading.Lock()

        def counted_blocking():
            with count_lock:
                thread_count[0] += 1
            worker_started.set()  # signal: thread is now running
            gate.wait(timeout=5.0)
            return {"status": "ok"}

        # First submit — acquires the atomic claim
        job_id_1 = self.manager.submit_job(
            "global_repo_refresh",
            counted_blocking,
            submitter_username="admin",
            is_admin=True,
            repo_alias="my-repo-global",
        )

        # Wait until the first worker is actually executing before second submit
        worker_started.wait(timeout=2.0)

        # Second submit — must raise DuplicateJobError
        with pytest.raises(DuplicateJobError) as exc_info:
            self.manager.submit_job(
                "global_repo_refresh",
                counted_blocking,
                submitter_username="admin",
                is_admin=True,
                repo_alias="my-repo-global",
            )

        # Verify only ONE thread ever ran
        assert thread_count[0] == 1, (
            f"Expected exactly 1 worker thread, got {thread_count[0]} "
            "(second submit spawned a duplicate worker — Bug #1065 not fixed)"
        )

        # Verify the error carries meaningful context
        assert exc_info.value.operation_type == "global_repo_refresh"
        assert exc_info.value.repo_alias == "my-repo-global"
        assert exc_info.value.existing_job_id == job_id_1

        # Release the gate so teardown is clean
        gate.set()
        time.sleep(0.05)

    # -----------------------------------------------------------------------
    # AC3: DuplicateJobError is NOT swallowed — hard reject before thread spawn
    # -----------------------------------------------------------------------
    def test_duplicate_raises_not_swallowed(self):
        """
        Regression: previously register_job (non-atomic) exceptions were swallowed
        with try/except, allowing the worker thread to start anyway. The fix MUST
        propagate the error as a hard raise, not log-and-continue.
        """
        gate = threading.Event()

        def blocking():
            gate.wait(timeout=5.0)
            return {}

        self.manager.submit_job(
            "add_golden_repo",
            blocking,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-x",
        )
        time.sleep(0.05)

        # This must propagate — not be caught inside submit_job and logged
        with pytest.raises(DuplicateJobError):
            self.manager.submit_job(
                "add_golden_repo",
                blocking,
                submitter_username="admin",
                is_admin=True,
                repo_alias="repo-x",
            )
        gate.set()

    # -----------------------------------------------------------------------
    # AC4: the DB-layer unique index is the actual gate (not just in-process)
    # -----------------------------------------------------------------------
    def test_db_level_unique_index_is_the_gate(self):
        """
        The fix must use the DB-layer atomic gate — not just the in-process lock.
        We verify this by manually inserting an 'active' row into the DB and then
        calling submit_job: it must raise DuplicateJobError even though the
        in-process BackgroundJobManager._lock dict is empty (simulates cross-node).
        """
        import uuid
        from datetime import datetime, timezone

        existing_job_id = str(uuid.uuid4())
        # Directly insert a 'pending' row into the DB, bypassing the in-process state
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO background_jobs
                   (job_id, operation_type, status, created_at, username,
                    is_admin, cancelled, repo_alias, resolution_attempts,
                    progress)
                   VALUES (?, ?, 'pending', ?, 'other-node', 0, 0, ?, 0, 0)""",
                (
                    existing_job_id,
                    "global_repo_refresh",
                    datetime.now(timezone.utc).isoformat(),
                    "cross-node-repo",
                ),
            )
            conn.commit()

        # Now submit_job from THIS node — the in-process dict is empty but
        # the DB-layer index should catch the duplicate
        with pytest.raises(DuplicateJobError) as exc_info:
            self.manager.submit_job(
                "global_repo_refresh",
                simple_job,
                submitter_username="admin",
                is_admin=True,
                repo_alias="cross-node-repo",
            )

        assert exc_info.value.existing_job_id == existing_job_id
        assert exc_info.value.repo_alias == "cross-node-repo"

    # -----------------------------------------------------------------------
    # AC5: different repo_alias — two submits for the same op succeed
    # -----------------------------------------------------------------------
    def test_same_op_different_repo_both_succeed(self):
        """Two submits for the same operation_type but different repo_alias are OK."""
        id1 = self.manager.submit_job(
            "global_repo_refresh",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-alpha",
        )
        id2 = self.manager.submit_job(
            "global_repo_refresh",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="repo-beta",
        )
        assert id1 != id2
        time.sleep(0.1)

    # -----------------------------------------------------------------------
    # AC6: no repo_alias → non-atomic path still works
    # -----------------------------------------------------------------------
    def test_submit_without_repo_alias_still_works(self):
        """submit_job without repo_alias must continue to work (non-repo-scoped)."""
        job_id = self.manager.submit_job(
            "provider_index_rebuild",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias=None,
        )
        assert isinstance(job_id, str) and len(job_id) > 0
        time.sleep(0.05)

    # -----------------------------------------------------------------------
    # AC7: after first job completes, the same op+repo can be submitted again
    # -----------------------------------------------------------------------
    def test_resubmit_after_completion_succeeds(self):
        """Once a job finishes, the unique constraint releases and the same
        (op_type, repo_alias) can be submitted again."""
        job_id_1 = self.manager.submit_job(
            "global_repo_refresh",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="my-repo-global",
        )
        # Wait for the first job to complete
        time.sleep(0.5)

        # Second submit must succeed (not raise DuplicateJobError)
        job_id_2 = self.manager.submit_job(
            "global_repo_refresh",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="my-repo-global",
        )
        assert job_id_1 != job_id_2
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Test class: concurrent-submit race — exactly one thread wins
# ---------------------------------------------------------------------------


class TestSubmitJobConcurrentRace:
    """
    Stress test: two threads simultaneously call submit_job for the same
    (operation_type, repo_alias). Exactly one must succeed; the other must
    raise DuplicateJobError. The worker must execute exactly once.
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = _make_db(Path(self.tmp))
        self.manager = _make_manager(self.db_path)

    def teardown_method(self):
        try:
            self.manager.shutdown()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_submit_exactly_one_winner(self):
        """
        Two concurrent submit_job calls for the same (op, repo) — exactly one
        proceeds, the other raises DuplicateJobError. Worker executes once only.

        The worker MUST block until both submitters have attempted, otherwise
        the fast completion would remove the partial-index row before the second
        submit arrives — making both look like "successes".
        """
        successes = []
        errors = []
        exec_count = [0]
        exec_lock = threading.Lock()

        # Holds the first worker alive while both threads submit
        worker_gate = threading.Event()
        # Ensures the first worker has started before the second thread submits
        worker_started = threading.Event()
        # Synchronises both caller threads at the submit point
        gate_start = threading.Barrier(2)

        def blocking_counted_job():
            with exec_lock:
                exec_count[0] += 1
            worker_started.set()  # signal: I am running
            worker_gate.wait(timeout=5.0)  # hold until both callers have submitted
            return {}

        def try_submit():
            gate_start.wait(timeout=5.0)  # both threads line up together
            try:
                jid = self.manager.submit_job(
                    "global_repo_refresh",
                    blocking_counted_job,
                    submitter_username="admin",
                    is_admin=True,
                    repo_alias="race-repo",
                )
                successes.append(jid)
            except DuplicateJobError as e:
                errors.append(e)

        t1 = threading.Thread(target=try_submit)
        t2 = threading.Thread(target=try_submit)
        t1.start()
        t2.start()

        # Wait until the first worker is actually running (job is in 'running'
        # state and the partial index row is firmly held)
        worker_started.wait(timeout=5.0)

        # Wait for both caller threads to finish submitting (one succeeds, one
        # raises DuplicateJobError)
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        # Now release the worker so it can complete
        worker_gate.set()
        time.sleep(0.3)

        assert len(successes) == 1, (
            f"Expected exactly 1 success, got {len(successes)}: {successes}"
        )
        assert len(errors) == 1, (
            f"Expected exactly 1 DuplicateJobError, got {len(errors)}"
        )
        assert exec_count[0] == 1, (
            f"Expected worker to execute exactly once, ran {exec_count[0]} times"
        )


# ---------------------------------------------------------------------------
# Regression tests: Bug #1065 review rejection — atomic path must persist
# actor_username and is_admin correctly (Story #1032 AC12 audit trail).
# ---------------------------------------------------------------------------


class TestAtomicPathColumnPersistence:
    """
    Regression for Bug #1065 code-review rejection:
    register_job_if_no_conflict's INSERT silently dropped actor_username
    (always None) and coerced is_admin to 0, defeating AC12 audit trail.

    These tests use a real BackgroundJobManager wired to a real JobTracker
    on a real SQLite DB (no mocks — anti-mock rule), read the DB row back
    after submit_job, and assert that actor_username and is_admin land with
    the values the caller supplied.
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = _make_db(Path(self.tmp))
        self.manager = _make_manager(self.db_path)

    def teardown_method(self):
        try:
            self.manager.shutdown()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_actor_username_persists_on_atomic_path(self):
        """
        AC12 regression: submit_job with a distinct actor_username on the
        atomic path (repo-scoped, tracker present) must persist actor_username
        in the DB row, not None.

        Before fix: register_job_if_no_conflict's INSERT did not include
        actor_username; _persist_job_to_sqlite took the UPDATE branch (row
        already existed) which never writes actor_username. Result: always None.
        """
        job_id = self.manager.submit_job(
            "deactivate_repository",
            simple_job,
            submitter_username="bob",
            is_admin=False,
            repo_alias="my-repo",
            actor_username="admin",
        )
        # Read back from DB directly — do not use in-memory state
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT actor_username FROM background_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None, f"Job {job_id} not found in DB"
        assert row[0] == "admin", (
            f"actor_username should be 'admin' on atomic path, got {row[0]!r}. "
            "Bug #1065 AC12: actor_username silently dropped by atomic INSERT."
        )

    def test_is_admin_persists_on_atomic_path(self):
        """
        Regression: register_job_if_no_conflict's SQLite INSERT hardcoded
        is_admin=0; the subsequent UPDATE path also never sets is_admin.
        Admin-submitted repo-scoped jobs ended up with is_admin=0 in the DB.
        """
        job_id = self.manager.submit_job(
            "add_golden_repo",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="r1",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT is_admin FROM background_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None, f"Job {job_id} not found in DB"
        assert row[0] == 1, (
            f"is_admin should be 1 for admin-submitted job on atomic path, got {row[0]}. "
            "Bug #1065: is_admin coerced to 0 by atomic INSERT hardcode."
        )


# ---------------------------------------------------------------------------
# Production-wired test: JobTracker WITH BackgroundJobsSqliteBackend.
# This matches the production configuration (service_init.py always provides
# a storage_backend, making _conn_manager=None). The previous _needs_reconcile
# block used hasattr(tracker, "_conn_manager") which returns True even when the
# value is None, then called None.execute_atomic(...) → AttributeError crash.
# ---------------------------------------------------------------------------


def _make_sqlite_backend(db_path: str):
    """Create a real BackgroundJobsSqliteBackend wired to the test DB."""
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    return BackgroundJobsSqliteBackend(db_path)


def _make_manager_with_backend(db_path: str) -> BackgroundJobManager:
    """
    Create a BackgroundJobManager wired to a JobTracker that HAS a
    storage_backend (production-equivalent: _conn_manager is None).
    """
    backend = _make_sqlite_backend(db_path)
    tracker = JobTracker(db_path=db_path, storage_backend=backend)
    manager = BackgroundJobManager(storage_path=None)
    manager._job_tracker = tracker  # type: ignore[assignment]
    return manager


class TestAtomicPathColumnPersistenceBackendWired:
    """
    Production-equivalent regression for Bug #1065 code-review rejection.

    JobTracker is wired WITH a storage_backend (making _conn_manager=None),
    matching how service_init.py builds it in production. The old reconcile
    block crashed here because hasattr(tracker, "_conn_manager") is True
    even when the value is None, causing None.execute_atomic(...).

    The correct fix threads is_admin and actor_username directly into
    register_job_if_no_conflict → _atomic_insert_impl so no second write
    is needed at all.
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = _make_db(Path(self.tmp))
        self.manager = _make_manager_with_backend(self.db_path)

    def teardown_method(self):
        try:
            self.manager.shutdown()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_actor_username_persists_on_backend_wired_path(self):
        """
        AC12 regression (production path): submit_job with a distinct
        actor_username on the backend-wired tracker must persist actor_username
        in the DB row, not None. Must NOT raise AttributeError.
        """
        job_id = self.manager.submit_job(
            "deactivate_repository",
            simple_job,
            submitter_username="bob",
            is_admin=False,
            repo_alias="prod-repo",
            actor_username="admin",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT actor_username FROM background_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None, f"Job {job_id} not found in DB"
        assert row[0] == "admin", (
            f"actor_username should be 'admin' on backend-wired path, got {row[0]!r}. "
            "The old reconcile block crashed here with AttributeError: "
            "'NoneType' object has no attribute 'execute_atomic'."
        )

    def test_is_admin_persists_on_backend_wired_path(self):
        """
        Regression (production path): is_admin must be 1 for admin-submitted
        repo-scoped jobs on the backend-wired tracker. Must NOT raise AttributeError.
        """
        job_id = self.manager.submit_job(
            "add_golden_repo",
            simple_job,
            submitter_username="admin",
            is_admin=True,
            repo_alias="prod-repo-2",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT is_admin FROM background_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None, f"Job {job_id} not found in DB"
        assert row[0] == 1, (
            f"is_admin should be 1 on backend-wired path, got {row[0]}. "
            "Bug #1065: is_admin not threaded into atomic INSERT."
        )


# ---------------------------------------------------------------------------
# RED test: BackgroundJobsSqliteBackend.save_job uses INSERT OR IGNORE which
# swallows the unique-index violation on the backend-wired path.
#
# Before the fix: _atomic_insert_impl's backend branch calls save_job() which
# uses INSERT OR IGNORE. When a duplicate pending/running row exists for the
# same (operation_type, repo_alias), save_job silently discards the INSERT
# (returns None, no exception). _atomic_insert_or_raise never catches an
# IntegrityError, so it never raises DuplicateJobError — two workers race.
#
# After the fix: _atomic_insert_impl routes through a new
# BackgroundJobsSqliteBackend.atomic_claim_insert() that uses plain INSERT
# (no OR IGNORE), so sqlite3.IntegrityError propagates and the caller
# translates it into DuplicateJobError.
# ---------------------------------------------------------------------------


class TestAtomicPathInsertOrIgnoreSwallowsViolation:
    """
    Proves that save_job(INSERT OR IGNORE) swallows the unique-index violation
    and that atomic_claim_insert(plain INSERT) surfaces it.

    Uses BackgroundJobsSqliteBackend + a real SQLite DB with idx_active_job_per_repo.
    No mocks — anti-mock rule.
    """

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = _make_db(Path(self.tmp))

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backend(self):
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        return BackgroundJobsSqliteBackend(self.db_path)

    def test_save_job_silently_ignores_duplicate_active_row(self):
        """
        Regression proof: save_job(INSERT OR IGNORE) must silently succeed
        (return None, no exception) even when a pending row with the same
        (operation_type, repo_alias) already holds the unique-index slot.

        This is the root cause of Bug #1065 on the backend-wired path:
        the atomic gate never fires because save_job swallows the violation.
        """
        import uuid
        from datetime import datetime, timezone

        backend = self._make_backend()

        job_id_1 = str(uuid.uuid4())
        created = datetime.now(timezone.utc).isoformat()

        # First INSERT — takes the idx_active_job_per_repo slot.
        backend.save_job(
            job_id=job_id_1,
            operation_type="global_repo_refresh",
            status="pending",
            created_at=created,
            username="admin",
            progress=0,
            repo_alias="repo-or-ignore",
        )

        # Second INSERT for the same (op, repo) — INSERT OR IGNORE silently
        # discards it; no exception, row count for job_id_2 stays 0.
        job_id_2 = str(uuid.uuid4())
        backend.save_job(
            job_id=job_id_2,
            operation_type="global_repo_refresh",
            status="pending",
            created_at=created,
            username="admin",
            progress=0,
            repo_alias="repo-or-ignore",
        )

        # Verify the second row was silently ignored (the root-cause behaviour).
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM background_jobs WHERE job_id = ?",
                (job_id_2,),
            ).fetchone()
        assert row[0] == 0, (
            "save_job(INSERT OR IGNORE) should have silently discarded the "
            "second pending row — this is the root-cause of Bug #1065 on the "
            "backend-wired path. If this assertion fails, save_job no longer "
            "uses INSERT OR IGNORE and the test needs updating."
        )

    def test_atomic_claim_insert_raises_on_duplicate_active_row(self):
        """
        GREEN proof: BackgroundJobsSqliteBackend.atomic_claim_insert() must use
        plain INSERT (no OR IGNORE) so sqlite3.IntegrityError propagates when
        idx_active_job_per_repo is violated.

        This test drives the implementation of atomic_claim_insert. It will
        FAIL (AttributeError) until atomic_claim_insert is added to the backend.
        """
        import uuid
        from datetime import datetime, timezone

        backend = self._make_backend()
        created = datetime.now(timezone.utc).isoformat()

        job_id_1 = str(uuid.uuid4())
        backend.atomic_claim_insert(
            job_id=job_id_1,
            operation_type="global_repo_refresh",
            status="pending",
            created_at=created,
            username="admin",
            progress=0,
            repo_alias="repo-plain-insert",
        )

        job_id_2 = str(uuid.uuid4())
        # Plain INSERT must raise sqlite3.IntegrityError (not silently ignore).
        with pytest.raises(Exception) as exc_info:
            backend.atomic_claim_insert(
                job_id=job_id_2,
                operation_type="global_repo_refresh",
                status="pending",
                created_at=created,
                username="admin",
                progress=0,
                repo_alias="repo-plain-insert",
            )
        # Must be an IntegrityError (or a wrapper of one).
        exc_type_name = type(exc_info.value).__name__
        assert "IntegrityError" in exc_type_name or "Integrity" in str(exc_info.value), (
            f"Expected IntegrityError from atomic_claim_insert, got "
            f"{exc_type_name}: {exc_info.value}"
        )
