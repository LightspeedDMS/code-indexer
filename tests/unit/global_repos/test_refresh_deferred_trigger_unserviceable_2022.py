"""Bug #2022 Gap 4, review round 6 item 4: a deferred trigger whose refresh
returns without publishing must not re-fire forever as a job that does
nothing.

- Integrity quarantine (needs an operator) and an orphaned clone (no source
  to index): the trigger is dropped.
- A write lock held by another writer is transient: the trigger is kept,
  but each such skip escalates its retry interval (bounded by the backoff
  cap), and it is delivered once the lock is released.

Real scheduler, registry, metadata stores (SQLite always, PostgreSQL with
``TEST_POSTGRES_DSN``), write-lock manager and git clone; only the
job-submission boundary is recorded.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Iterator, Tuple

import pytest

from code_indexer.global_repos.refresh_failure_recovery import (
    REFRESH_INTEGRITY_QUARANTINE_THRESHOLD,
    RefreshDeferredError,
)
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    REPO,
    Harness,
    RecordingJobManager,
    build_harness,
    run_one_scheduler_iteration,
)

ONE_DAY_SECONDS = 24 * 3600
OTHER_WRITER = "langfuse_sync"


@pytest.fixture(params=STORE_KINDS)
def metadata(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _deferred(
    tmp_path: Path, metadata: Any, git_remote: bool = False
) -> Tuple[Harness, RecordingJobManager]:
    harness = build_harness(
        tmp_path, metadata, snapshot_mode="clean", git_remote=git_remote
    )
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    with pytest.raises(RefreshDeferredError):
        harness.scheduler.trigger_refresh_for_repo(ALIAS)
    return harness, jobs


def _pass_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + ONE_DAY_SECONDS)


def _pending(metadata: Any) -> bool:
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    return state is not None and bool(state["pending_trigger"])


def test_quarantined_alias_drops_its_deferred_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    for _ in range(REFRESH_INTEGRITY_QUARANTINE_THRESHOLD):
        metadata.record_refresh_integrity_failure(ALIAS, "corrupt chunks.db")

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert result.get("skipped") == "integrity_quarantined", result
    assert not _pending(metadata), "a quarantined alias keeps re-firing"
    _pass_the_backoff(monkeypatch)
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == []


def test_orphaned_alias_drops_its_deferred_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _deferred(tmp_path, metadata, git_remote=True)
    shutil.rmtree(harness.source)  # registry row present, clone gone

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert "Orphaned" in str(result.get("message")), result
    assert not _pending(metadata), "an orphaned alias keeps re-firing"
    _pass_the_backoff(monkeypatch)
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == []


def test_held_write_lock_keeps_the_trigger_and_escalates(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    before = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert harness.scheduler.acquire_write_lock(REPO, owner_name=OTHER_WRITER)

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert result.get("message") == "Skipped, write lock held", result
    after = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert after["pending_trigger"] is True, "a transient skip dropped the trigger"
    assert (
        after["consecutive_failure_count"] == before["consecutive_failure_count"] + 1
    ), "retries after a held lock never escalate"

    harness.scheduler.release_write_lock(REPO, owner_name=OTHER_WRITER)
    _pass_the_backoff(monkeypatch)
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS], "the trigger was not delivered after release"
