"""The cidx-meta debouncer settles a write only on a job that covers it:
an older job tracked late never settles a newer write, and a submission the
scheduler suppressed (nothing ran) never settles anything.

Timers are never waited on: each step runs the pending timer's callback.
Ordering between writers is driven with threads and events, never sleeps.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from code_indexer.global_repos.cidx_meta_refresh_debouncer import (
    CidxMetaRefreshDebouncer,
)
from code_indexer.global_repos.meta_description_hook import request_cidx_meta_refresh
from tests.utils.fatal_chunk_store_fixtures import cidx_child_command, child_env
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fatal_store_harness import (
    Harness,
    RecordingJobManager,
    build_harness,
)

DEBOUNCE_SECONDS = 30.0  # never elapses during a test
WAIT_SECONDS = 30.0
META_ALIAS = "cidx-meta-global"
COVERING = {"success": True, "message": "Refresh complete", "covered_source": True}


def _expire_now(debouncer: CidxMetaRefreshDebouncer) -> None:
    with debouncer._lock:
        pending: Optional[threading.Timer] = debouncer._timer
    assert pending is not None, "the refresh is owed but no timer is pending"
    pending.cancel()
    pending.function(*pending.args, **pending.kwargs)


class _PausingJobStore:
    """Submits refresh jobs into a status table the test fills in. The first
    submission pauses its caller after the job exists and before the call
    returns -- a writer that submitted but has not tracked its job yet."""

    def __init__(self) -> None:
        self.statuses: Dict[str, Dict[str, Any]] = {}
        self.submitted: List[str] = []
        self.first_submitted = threading.Event()
        self.release_first = threading.Event()
        self.background_job_manager = self

    def trigger_refresh_for_repo(self, alias_name: str) -> str:
        job_id = f"job-{len(self.submitted) + 1}"
        self.submitted.append(job_id)
        self.statuses[job_id] = {"status": "running", "result": None}
        if job_id == "job-1":
            self.first_submitted.set()
            assert self.release_first.wait(WAIT_SECONDS)
        return job_id

    def get_job_status(
        self, job_id: str, username: str, is_admin: bool = False
    ) -> Optional[Dict[str, Any]]:
        return self.statuses.get(job_id)

    def finish(self, job_id: str) -> None:
        self.statuses[job_id] = {"status": "completed", "result": COVERING}


def test_an_older_job_tracked_late_never_settles_a_newer_write() -> None:
    store = _PausingJobStore()
    debouncer = CidxMetaRefreshDebouncer(store, debounce_seconds=DEBOUNCE_SECONDS)
    writer_a = threading.Thread(
        target=request_cidx_meta_refresh, args=(store, debouncer)
    )
    try:
        writer_a.start()
        assert store.first_submitted.wait(WAIT_SECONDS)
        store.finish("job-1")  # A's job completes before A tracks it
        request_cidx_meta_refresh(store, debouncer)  # writer B: job-2, tracked
        store.release_first.set()  # A now tracks its older job
        writer_a.join(WAIT_SECONDS)
        assert not writer_a.is_alive()

        _expire_now(debouncer)  # job-2 is still running

        assert debouncer._dirty is True, "B's write settled by A's job"
        store.finish("job-2")
        _expire_now(debouncer)
        assert debouncer._dirty is False
        assert store.submitted == ["job-1", "job-2"]
    finally:
        store.release_first.set()
        debouncer.shutdown()


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        yield backend


def _register_catalog(harness: Harness, initialized: bool) -> Path:
    """A ``local://cidx-meta`` alias; *initialized* gives it a
    ``.code-indexer/`` without a valid config (a failed repair)."""
    meta = harness.source.parent / "cidx-meta"
    meta.mkdir(parents=True)
    if initialized:
        (meta / ".code-indexer").mkdir()
    harness.registry.register_global_repo(
        "cidx-meta",
        META_ALIAS,
        "local://cidx-meta",
        index_path=str(meta),
        allow_reserved=True,
    )
    return meta


def _quarantine(harness: Harness) -> None:
    for _ in range(3):
        harness.scheduler._record_local_repo_repair_failure(META_ALIAS, "failed")


def test_no_job_manager_quarantined_refresh_keeps_the_write_owed(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _register_catalog(harness, initialized=True)
    _quarantine(harness)
    scheduler = harness.scheduler
    assert scheduler.background_job_manager is None  # runs refreshes in-process
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        request_cidx_meta_refresh(scheduler, debouncer)
        with debouncer._lock:
            assert debouncer._dirty is True, "a quarantine-skipped run settled"

        _expire_now(debouncer)  # still quarantined: retried, paced
        with debouncer._lock:
            assert debouncer._dirty is True
            assert debouncer._timer is not None
            assert debouncer._timer.interval == 2 * DEBOUNCE_SECONDS
    finally:
        debouncer.shutdown()


def test_no_job_manager_refresh_run_in_process_settles(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _register_catalog(harness, initialized=False)
    scheduler = harness.scheduler
    assert scheduler.background_job_manager is None
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        request_cidx_meta_refresh(scheduler, debouncer)
        with debouncer._lock:
            assert debouncer._dirty is False
            assert debouncer._timer is None
    finally:
        debouncer.shutdown()


def test_a_quarantine_suppressed_submission_keeps_the_write_owed(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    meta = _register_catalog(harness, initialized=True)
    scheduler = harness.scheduler
    jobs = RecordingJobManager()
    scheduler.background_job_manager = jobs  # type: ignore[assignment]
    _quarantine(harness)
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        request_cidx_meta_refresh(scheduler, debouncer)
        assert jobs.submitted == [], "the quarantine did not suppress it"
        with debouncer._lock:
            assert debouncer._dirty is True, "a suppressed submission settled"

        _expire_now(debouncer)  # still quarantined: suppressed again, paced
        assert jobs.submitted == []
        with debouncer._lock:
            assert debouncer._dirty is True
            assert debouncer._timer is not None
            assert debouncer._timer.interval == 2 * DEBOUNCE_SECONDS

        subprocess.run(  # the repair succeeds out of band
            cidx_child_command(["init"]),
            cwd=meta,
            env=child_env(),
            check=True,
            capture_output=True,
            timeout=180,
        )
        _expire_now(debouncer)  # submitted now
        assert jobs.submitted == [META_ALIAS]
        _expire_now(debouncer)  # the job store shows it covered the write
        with debouncer._lock:
            assert debouncer._dirty is False
    finally:
        debouncer.shutdown()
