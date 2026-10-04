"""Bug #2022 Gap 4, review round 6 item 4: a deferred trigger whose refresh
returns without publishing must not re-fire forever as a job that does
nothing.

- Unserviceable (needs an operator): integrity or local-repair quarantine,
  an orphaned clone, the alias or registry entry gone. The trigger is
  dropped -- only the generation captured before the refresh ran, so a
  trigger deferred meanwhile survives.
- Recoverable (a held write lock, a local repo not yet initialized): the
  trigger is kept and each such skip escalates its retry interval (bounded
  by the backoff cap) in one generation-conditional statement that keeps
  the original failure reason; it is delivered once the cause clears. With
  no pending trigger nothing changes.

Real scheduler, registry, metadata stores (SQLite always, PostgreSQL with
``TEST_POSTGRES_DSN``), write-lock manager and git clone; only the
job-submission boundary is recorded.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, Optional, Tuple

import pytest

if TYPE_CHECKING:
    from code_indexer.server.storage.protocols.golden_repo_metadata_backend import (
        GoldenRepoMetadataBackend,
    )

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


class _OperatorActsRightAfterTheQuarantineRead:
    """The real store, except that right after the scheduler reads the
    quarantine state (its skip decision) a concurrent event runs."""

    def __init__(self, real: "GoldenRepoMetadataBackend") -> None:
        self._real = real
        self.concurrent_event: Optional[Callable[[], None]] = None

    def get_refresh_integrity_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        state = self._real.get_refresh_integrity_failure_state(golden_alias)
        event, self.concurrent_event = self.concurrent_event, None
        if event is not None:
            event()
        return state

    def __getattr__(self, name: str) -> Any:  # forwards any protocol member
        return getattr(self._real, name)


def test_stale_quarantine_skip_never_drops_a_newer_trigger(
    tmp_path: Path, metadata: Any
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    for _ in range(REFRESH_INTEGRITY_QUARANTINE_THRESHOLD):
        metadata.record_refresh_integrity_failure(ALIAS, "corrupt chunks.db")
    store = _OperatorActsRightAfterTheQuarantineRead(metadata)
    harness.scheduler.golden_repo_metadata = store
    newer: Dict[str, Any] = {}

    def _operator_clears_and_a_write_arrives() -> None:
        metadata.reset_refresh_integrity_failure(ALIAS)
        metadata.record_refresh_failure_backoff(ALIAS, "disk full")
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(ALIAS)
        newer.update(metadata.get_refresh_failure_backoff_state(ALIAS))

    store.concurrent_event = _operator_clears_and_a_write_arrives

    result = harness.scheduler._execute_refresh(ALIAS)  # the stale job

    assert result.get("skipped") == "integrity_quarantined", result
    after = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert after["pending_trigger"] is True, "a stale skip dropped a newer trigger"
    assert after["trigger_generation"] == newer["trigger_generation"]


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


def test_held_write_lock_escalation_keeps_the_original_failure_reason(
    tmp_path: Path, metadata: Any
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)  # failed with "disk full"
    assert harness.scheduler.acquire_write_lock(REPO, owner_name=OTHER_WRITER)
    try:
        result = harness.scheduler._execute_refresh(ALIAS)
    finally:
        harness.scheduler.release_write_lock(REPO, owner_name=OTHER_WRITER)

    assert result.get("message") == "Skipped, write lock held", result
    after = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert after["last_detail"] == "disk full", "escalation hid the failure reason"


def test_held_write_lock_without_a_pending_trigger_changes_nothing(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")  # no trigger
    before = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert harness.scheduler.acquire_write_lock(REPO, owner_name=OTHER_WRITER)
    try:
        result = harness.scheduler._execute_refresh(ALIAS)
    finally:
        harness.scheduler.release_write_lock(REPO, owner_name=OTHER_WRITER)

    assert result.get("message") == "Skipped, write lock held", result
    after = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert after["consecutive_failure_count"] == before["consecutive_failure_count"]


def _assert_dropped_and_never_refires(
    harness: Harness,
    jobs: RecordingJobManager,
    metadata: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not _pending(metadata), "an unserviceable skip keeps re-firing"
    _pass_the_backoff(monkeypatch)
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == []


def test_local_repo_repair_quarantine_drops_its_deferred_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    for _ in range(REFRESH_INTEGRITY_QUARANTINE_THRESHOLD):
        metadata.record_local_repo_repair_failure(ALIAS, "cidx init failed")
    (harness.source / ".code-indexer" / "config.json").unlink()

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert result.get("skipped") == "local_repo_repair_quarantined", result
    _assert_dropped_and_never_refires(harness, jobs, metadata, monkeypatch)


def test_alias_gone_drops_its_deferred_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    harness.scheduler.alias_manager.delete_alias(ALIAS)

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert result.get("message") == "Alias not found, skipped", result
    _assert_dropped_and_never_refires(harness, jobs, metadata, monkeypatch)


def test_registry_entry_gone_drops_its_deferred_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    harness.registry.unregister_global_repo(ALIAS)

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert result.get("message") == "Repo not in registry, skipped", result
    _assert_dropped_and_never_refires(harness, jobs, metadata, monkeypatch)


def test_uninitialized_local_repo_keeps_the_trigger_and_escalates(
    tmp_path: Path, metadata: Any
) -> None:
    harness, jobs = _deferred(tmp_path, metadata)
    before = metadata.get_refresh_failure_backoff_state(ALIAS)
    shutil.rmtree(harness.source / ".code-indexer")  # its writer has not run yet

    result = harness.scheduler._execute_refresh(ALIAS)  # the fired job

    assert result.get("message") == "Not yet initialized, skipped", result
    after = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert after["pending_trigger"] is True, "a recoverable skip dropped the trigger"
    assert (
        after["consecutive_failure_count"] == before["consecutive_failure_count"] + 1
    ), "retries of a recoverable skip never escalate"
