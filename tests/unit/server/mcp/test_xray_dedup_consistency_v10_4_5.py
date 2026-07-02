"""v10.4.5 tests for Defect 2: xray_search and xray_explore dedup gate consistency.

Investigation result:
  Both handle_xray_search and handle_xray_explore call submit_job with repo_alias=single_alias.
  BackgroundJobManager._check_operation_conflict checks PENDING/RUNNING jobs only.
  The field-test "inconsistency" was a timing artifact: xray_search jobs on tiny repos
  completed in <1s before the second submit, so the dedup gate never fired.
  The code is CONSISTENT — both handlers use the same dedup path.

Job lifecycle management:
  Tests use threading.Event to hold jobs in RUNNING state deterministically.
  release_event.wait() has NO timeout — jobs block until explicitly released.
  After asserting dedup behaviour, release_event.set() unblocks all held jobs.
  _wait_for_completion raises AssertionError on timeout to guarantee no silent leaks.

Shared helpers declared:
- _make_user(username, role) — user factory
- _make_bjm() — BackgroundJobManager with in-memory backend
- _make_event_job(release_event) — returns Callable[[Any], Dict] blocking on event
- _noop_job(progress_callback) — job function that completes immediately
- _run_concurrent_submits(threads, result_queue) — start/stagger/join/drain len(threads)
- _wait_for_completion(bjm, job_id, username) — poll to completed; AssertionError on timeout
- _assert_duplicate_for_same_operation(operation_type) — shared concurrent-submit logic
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    DuplicateJobError,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

THREAD_START_GAP_SECONDS = 0.05  # stagger between thread starts
THREAD_JOIN_TIMEOUT_SECONDS = 5  # max join-wait per thread
COMPLETION_TIMEOUT_SECONDS = 5.0  # max poll-wait for a job to reach completed
POLL_INTERVAL_SECONDS = 0.05  # polling cadence for job status
CREATED_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(
    username: str = "testuser", role: UserRole = UserRole.NORMAL_USER
) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=CREATED_AT,
    )


def _make_bjm() -> BackgroundJobManager:
    """In-memory BackgroundJobManager — no SQLite, no disk I/O."""
    from code_indexer.server.utils.config_manager import ServerResourceConfig

    return BackgroundJobManager(resource_config=ServerResourceConfig())


def _make_event_job(release_event: threading.Event) -> Callable[[Any], Dict[str, Any]]:
    """Return a job function that blocks on release_event with no timeout.

    The job stays RUNNING until the test calls release_event.set(), making
    the lifecycle fully event-driven and deterministic.
    """

    def _job(progress_callback: Any) -> Dict[str, Any]:
        release_event.wait()  # no timeout — blocks until explicitly released
        return {"status": "done"}

    return _job


def _noop_job(progress_callback: Any) -> Dict[str, Any]:
    """Job function that completes immediately."""
    return {"status": "done"}


def _run_concurrent_submits(
    threads: List[threading.Thread], result_queue: "queue.Queue[str]"
) -> List[str]:
    """Start threads with a stagger gap, join all, drain len(threads) results.

    Drains exactly len(threads) items so the helper is correct for any thread count.
    """
    for i, t in enumerate(threads):
        t.start()
        if i < len(threads) - 1:
            time.sleep(THREAD_START_GAP_SECONDS)
    for t in threads:
        t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
    return sorted(
        result_queue.get(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        for _ in range(len(threads))
    )


def _wait_for_completion(bjm: BackgroundJobManager, job_id: str, username: str) -> None:
    """Poll job_id until completed/failed. Raises AssertionError on timeout."""
    deadline = time.monotonic() + COMPLETION_TIMEOUT_SECONDS
    last_status: Any = None
    while time.monotonic() < deadline:
        last_status = bjm.get_job_status(job_id, username)
        if last_status and last_status.get("status") in ("completed", "failed"):
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"Job {job_id} did not reach completed/failed within "
        f"{COMPLETION_TIMEOUT_SECONDS}s. Last status: {last_status}"
    )


def _assert_duplicate_for_same_operation(operation_type: str) -> None:
    """Submit two concurrent jobs of the same operation_type against the same repo.

    Holds the first job RUNNING via a threading.Event, asserts dedup behaviour,
    then releases the event and waits for all jobs to finish.
    """
    user = _make_user()
    bjm = _make_bjm()
    result_queue: queue.Queue = queue.Queue()
    release_event = threading.Event()
    submitted_job_ids: List[str] = []
    ids_lock = threading.Lock()

    def _submit() -> None:
        try:
            job_id = bjm.submit_job(
                operation_type=operation_type,
                func=_make_event_job(release_event),
                submitter_username=user.username,
                repo_alias="test-repo-global",
            )
            with ids_lock:
                submitted_job_ids.append(job_id)
            result_queue.put("ok")
        except DuplicateJobError:
            result_queue.put("duplicate")

    threads = [threading.Thread(target=_submit), threading.Thread(target=_submit)]
    results = _run_concurrent_submits(threads, result_queue)

    assert results == ["duplicate", "ok"], (
        f"Expected one 'ok' and one 'duplicate' for {operation_type!r}, got: {results}"
    )

    # Release held jobs and assert clean completion — no leaked background work
    release_event.set()
    for job_id in submitted_job_ids:
        _wait_for_completion(bjm, job_id, user.username)


# ---------------------------------------------------------------------------
# AC1: two concurrent xray_search submits -> one DuplicateJobError
# ---------------------------------------------------------------------------


def test_xray_search_two_concurrent_same_repo_raises_duplicate():
    """Two concurrent xray_search submits against same repo -> exactly one DuplicateJobError."""
    _assert_duplicate_for_same_operation("xray_search")


# ---------------------------------------------------------------------------
# AC2: two concurrent xray_explore submits -> same dedup behaviour
# ---------------------------------------------------------------------------


def test_xray_explore_two_concurrent_same_repo_raises_duplicate():
    """Two concurrent xray_explore submits against same repo -> exactly one DuplicateJobError."""
    _assert_duplicate_for_same_operation("xray_explore")


# ---------------------------------------------------------------------------
# AC3: submit after completion succeeds (no false dedup)
# ---------------------------------------------------------------------------


def test_xray_search_after_completion_succeeds():
    """Submit xray_search -> wait for completion -> submit again -> both succeed."""
    user = _make_user()
    bjm = _make_bjm()

    job_id1 = bjm.submit_job(
        operation_type="xray_search",
        func=_noop_job,
        submitter_username=user.username,
        repo_alias="test-repo-global",
    )
    _wait_for_completion(bjm, job_id1, user.username)

    job_id2 = bjm.submit_job(
        operation_type="xray_search",
        func=_noop_job,
        submitter_username=user.username,
        repo_alias="test-repo-global",
    )
    _wait_for_completion(bjm, job_id2, user.username)

    assert job_id1 != job_id2


# ---------------------------------------------------------------------------
# AC4: different operation_types on same alias -> both succeed
# ---------------------------------------------------------------------------


def test_dedup_is_per_operation_type():
    """xray_search and xray_explore concurrently against same repo -> both succeed."""
    user = _make_user()
    bjm = _make_bjm()
    result_queue: queue.Queue = queue.Queue()
    release_event = threading.Event()
    submitted_job_ids: List[str] = []
    ids_lock = threading.Lock()

    def _submit(op_type: str) -> None:
        try:
            job_id = bjm.submit_job(
                operation_type=op_type,
                func=_make_event_job(release_event),
                submitter_username=user.username,
                repo_alias="test-repo-global",
            )
            with ids_lock:
                submitted_job_ids.append(job_id)
            result_queue.put("ok")
        except DuplicateJobError:
            result_queue.put("duplicate")

    threads = [
        threading.Thread(target=_submit, args=("xray_search",)),
        threading.Thread(target=_submit, args=("xray_explore",)),
    ]
    results = _run_concurrent_submits(threads, result_queue)

    assert results == ["ok", "ok"], (
        f"Different operation_types must not block each other. Got: {results}"
    )

    release_event.set()
    for job_id in submitted_job_ids:
        _wait_for_completion(bjm, job_id, user.username)
