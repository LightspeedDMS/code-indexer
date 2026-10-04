"""Bug #2022 Gap 4, review round 4: a system refresh trigger deferred by the
persisted failure backoff is delivered losslessly.

- A scheduler leases a due trigger; the trigger stays pending until a
  verified publish resolves it, so process death between the lease and the
  submission, or a submitted job orphaned by a restart, only delays it.
- A pass that sees the job still in flight neither writes nor logs.
- A trigger marked against a backoff that a concurrent success just resolved
  is submitted, not lost.
- Externally-managed mode has no scheduler loop to fire triggers, so it
  never defers them.
- A trigger deferred while a refresh is publishing survives that publish.

Real scheduler, real registry, real metadata stores (SQLite always,
PostgreSQL with ``TEST_POSTGRES_DSN``). Only the job-submission boundary is
recorded (``RecordingJobManager``) and the embedding child is replaced.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterator, List, Tuple
from unittest.mock import patch

import pytest

from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    RecordingJobManager,
    build_harness,
    run_one_scheduler_iteration,
)

ONE_DAY_SECONDS = 24 * 3600


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


class _Clock:
    """Moves wall-clock time (``time.time``) forward for the code under test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._real = time.time
        self.offset = 0.0
        monkeypatch.setattr(time, "time", lambda: self._real() + self.offset)

    def pass_the_backoff(self) -> None:
        self.offset += ONE_DAY_SECONDS


class _ProcessDeath(BaseException):
    """Ends the process mid-submission: nothing after it runs."""


def _deferred_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Harness, RecordingJobManager, _Clock]:
    """A backed-off local alias whose system trigger was deferred."""
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    with pytest.raises(RefreshDeferredError):
        harness.scheduler.trigger_refresh_for_repo(ALIAS)
    assert jobs.submitted == []
    return harness, jobs, _Clock(monkeypatch)


def test_trigger_survives_process_death_between_lease_and_submit(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    clock.pass_the_backoff()

    def _die(alias_name: str, *args: Any, **kwargs: Any) -> Any:
        raise _ProcessDeath()

    with monkeypatch.context() as dying:
        dying.setattr(harness.scheduler, "_submit_refresh_job", _die)
        with pytest.raises(_ProcessDeath):
            run_one_scheduler_iteration(harness)
    assert jobs.submitted == []

    clock.pass_the_backoff()  # the restarted scheduler, after the lease
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "a trigger lost to process death"


def test_trigger_survives_an_orphaned_job(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    clock.pass_the_backoff()
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS]

    # The job never completes (a restart fails it as orphaned).
    clock.pass_the_backoff()
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS, ALIAS], "a trigger lost with its orphaned job"


def test_passes_while_the_job_is_in_flight_are_quiet(
    tmp_path: Path,
    metadata: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    clock.pass_the_backoff()
    run_one_scheduler_iteration(harness)
    jobs.in_flight.add(ALIAS)
    state_after_fire = metadata.get_refresh_failure_backoff_state(ALIAS)

    attempts_after_fire = list(jobs.attempts)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        run_one_scheduler_iteration(harness)  # inside the lease
        assert jobs.attempts == attempts_after_fire, "a pass inside the lease"
        clock.pass_the_backoff()
        run_one_scheduler_iteration(harness)  # lease over, job still running

    assert jobs.attempts == attempts_after_fire + [ALIAS], "one dedup no-op"
    assert jobs.submitted == [ALIAS]
    noisy = [r.getMessage() for r in caplog.records if ALIAS in r.getMessage()]
    assert noisy == [], f"in-flight passes logged: {noisy}"
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state["pending_trigger"] is True
    assert (
        state["consecutive_failure_count"]
        == state_after_fire["consecutive_failure_count"]
    )


class _ResolvedJustBeforeMark:
    """The real store, except that a concurrent verified publish resolves the
    alias's backoff between the deferral's read and its mark."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def mark_refresh_trigger_pending(self, golden_alias: str, *args: Any) -> Any:
        state = self._real.get_refresh_failure_backoff_state(golden_alias)
        covered = state["trigger_generation"]  # that publish covered all so far
        self._real.resolve_refresh_failure_backoff(golden_alias, covered)
        return self._real.mark_refresh_trigger_pending(golden_alias, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_trigger_racing_a_resolved_backoff_is_submitted(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    harness.scheduler.golden_repo_metadata = _ResolvedJustBeforeMark(metadata)

    harness.scheduler.trigger_refresh_for_repo(ALIAS)

    assert jobs.submitted == [ALIAS], "a trigger lost to a concurrent success"
    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None


def test_externally_managed_mode_submits_instead_of_deferring(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    # The real flag, read through the real GlobalRepoOperations config path.
    from types import SimpleNamespace

    from code_indexer.global_repos.shared_operations import GlobalRepoOperations
    from code_indexer.server.utils.config_manager import ServerConfig

    config = ServerConfig(server_dir=str(tmp_path / "server"))
    assert config.golden_repos_config is not None  # set by __post_init__
    config.golden_repos_config.externally_managed = True
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: SimpleNamespace(get_config=lambda: config),
    )
    harness.scheduler.config_source = GlobalRepoOperations(str(tmp_path))
    assert harness.scheduler._is_externally_managed() is True

    harness.scheduler.trigger_refresh_for_repo(ALIAS)

    assert jobs.submitted == [ALIAS], "deferred with no loop to fire it"
    assert metadata.get_refresh_failure_backoff_state(ALIAS)["pending_trigger"] is False


def test_failed_submission_keeps_the_trigger(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    clock.pass_the_backoff()

    def _tracker_down(alias_name: str, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("job tracker unavailable")

    with monkeypatch.context() as failing:
        failing.setattr(harness.scheduler, "_submit_refresh_job", _tracker_down)
        run_one_scheduler_iteration(harness)
    assert jobs.submitted == []

    clock.pass_the_backoff()
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "a failed submission dropped the trigger"


class _LeaseFailsFor:
    """The real store, except that leasing one alias's trigger raises."""

    def __init__(self, real: Any, broken_alias: str) -> None:
        self._real = real
        self._broken_alias = broken_alias

    def lease_pending_refresh_trigger(self, golden_alias: str, *args: Any) -> Any:
        if golden_alias == self._broken_alias:
            raise OSError("store unavailable for this row")
        return self._real.lease_pending_refresh_trigger(golden_alias, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_store_error_for_one_trigger_spares_the_others(
    tmp_path: Path,
    metadata: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    broken = "broken-repo-global"
    metadata.record_refresh_failure_backoff(broken, "disk full")
    metadata.mark_refresh_trigger_pending(broken, 0.0)  # due before ALIAS
    harness.scheduler.golden_repo_metadata = _LeaseFailsFor(metadata, broken)
    clock.pass_the_backoff()

    with caplog.at_level(logging.ERROR):
        run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "one row's store error blocked the others"
    assert any(broken in r.getMessage() for r in caplog.records)


class _ListingFails:
    """The real store, except that listing the due triggers raises."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def list_due_refresh_triggers(self, now: float) -> Any:
        raise OSError("store unavailable")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_store_error_listing_triggers_does_not_fail_the_iteration(
    tmp_path: Path, metadata: Any, caplog: pytest.LogCaptureFixture
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    harness.scheduler.golden_repo_metadata = _ListingFails(metadata)

    with caplog.at_level(logging.ERROR):
        run_one_scheduler_iteration(harness)

    messages = [r.getMessage() for r in caplog.records]
    assert not any("scheduler iteration failed" in m for m in messages), messages
    assert any("listing due deferred refreshes failed" in m for m in messages)


def test_trigger_deferred_during_a_publishing_cycle_survives_it(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")

    def _index_while_a_trace_write_arrives(*args: Any, **kwargs: Any) -> None:
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(ALIAS)

    with patch.object(
        harness.scheduler,
        "_index_source",
        side_effect=_index_while_a_trace_write_arrives,
    ):
        result = harness.scheduler._execute_refresh(ALIAS)
    assert result.get("message") == "Refresh complete", result

    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "the publish swallowed a newer trigger"


#: Cross-node clock skew observed on a staging VM.
MARKER_CLOCK_LAG_SECONDS = 70.0
_INDEX_CHILD = (
    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
)


def test_trigger_deferred_during_a_cycle_survives_a_lagging_marker_clock(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    clock = _Clock(monkeypatch)

    def _index_child(command: List[str], **kwargs: object) -> int:
        # While the child indexes the source, a trace write arrives from a
        # node whose clock runs behind the refreshing node's.
        clock.offset = -MARKER_CLOCK_LAG_SECONDS
        try:
            with pytest.raises(RefreshDeferredError):
                harness.scheduler.trigger_refresh_for_repo(ALIAS)
        finally:
            clock.offset = 0.0
        return 0

    with patch(_INDEX_CHILD, side_effect=_index_child):
        result = harness.scheduler._execute_refresh(ALIAS)
    assert result.get("message") == "Refresh complete", result

    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "clock skew let the publish swallow a trigger"


def test_trigger_deferred_during_the_git_sync_survives_the_publish(
    tmp_path: Path, metadata: Any
) -> None:
    from code_indexer.global_repos.git_pull_updater import GitPullUpdater

    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    real_has_changes = GitPullUpdater.has_changes

    def _fetch_while_a_trace_write_arrives(updater: GitPullUpdater) -> bool:
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(ALIAS)
        return real_has_changes(updater)  # the real git fetch and compare

    with (
        patch.object(GitPullUpdater, "has_changes", _fetch_while_a_trace_write_arrives),
        patch(_INDEX_CHILD, return_value=0),
    ):
        result = harness.scheduler._execute_refresh(ALIAS)
    assert result.get("message") == "Refresh complete", result

    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "a trigger deferred during the git sync was lost"


def test_fired_triggers_publish_resolves_it_for_good(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    clock.pass_the_backoff()
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS]

    with patch(_INDEX_CHILD, return_value=0):  # the fired job runs
        result = harness.scheduler._execute_refresh(ALIAS)
    assert result.get("message") == "Refresh complete", result
    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None

    clock.pass_the_backoff()
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS], "a resolved trigger fired again"


#: Slack between the scheduler pass's clock read and this test's.
LEASE_TOLERANCE_SECONDS = 5.0


def test_lease_lasts_the_aliases_backoff_interval(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos.refresh_failure_recovery import (
        failure_backoff_seconds,
    )

    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    clock.pass_the_backoff()
    fired_at = time.time()
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS]

    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    expected = failure_backoff_seconds(int(state["consecutive_failure_count"]))
    assert state["pending_due_at"] - fired_at == pytest.approx(
        expected, abs=LEASE_TOLERANCE_SECONDS
    )


class _AnotherSchedulerLeasesFirst:
    """The real store, except that another scheduler leases every due
    trigger between this pass's listing and its own lease."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def list_due_refresh_triggers(self, now: float) -> Any:
        due = self._real.list_due_refresh_triggers(now)
        for state in due:
            assert self._real.lease_pending_refresh_trigger(
                state["golden_alias"], now, now + ONE_DAY_SECONDS
            )
        return due

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_trigger_leased_by_another_scheduler_is_not_submitted(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, clock = _deferred_trigger(tmp_path, metadata, monkeypatch)
    harness.scheduler.golden_repo_metadata = _AnotherSchedulerLeasesFirst(metadata)
    clock.pass_the_backoff()

    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [], "a trigger another scheduler leased was submitted"
