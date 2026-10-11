"""Bug #2125: a refresh that leaves work owed is retried automatically --
without waiting for new data, on a bounded escalating schedule, through
the same system gate (failure backoff, then the Story #2118 volume alarm)
as any system refresh -- and an owed retry is NOT a failure: new-data
triggers and the git schedule keep running promptly.

(A) a trigger-only (``local://``) repository whose refresh fails with an
    ORDINARY indexing error (the scheduler loop never schedules it);
(B) a refresh whose child exits 0 but records failed files: published,
    reported partial, and its still-existing recorded files retried.

Real scheduler, registry, snapshot manager and metadata store (SQLite
always, PostgreSQL with ``TEST_POSTGRES_DSN``). The indexing child is a
real process whose ``index`` outcome is scripted (``fake_index_child``);
only the job submission boundary is recorded (``RecordingJobManager``).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.global_repos import refresh_failure_recovery as recovery
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.index_spend_alarm import (
    DEFERRAL_SECONDS,
    IndexSpendAlarm,
)
from tests.utils.fake_index_child import (
    PARTIAL_FAILED_FILES,
    STALE_FAILED_FILES,
    FakeIndexChild,
    install_fake_index_child,
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
RUNAWAY_ITEMS = 50_000


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeIndexChild:
    fake = install_fake_index_child(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    return fake


class _Clock:
    """Moves wall-clock time (``time.time``) forward for the code under test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._real = time.time
        self.offset = 0.0
        monkeypatch.setattr(time, "time", lambda: self._real() + self.offset)


def _fail_refresh(harness: Harness, child: FakeIndexChild) -> None:
    child.set_mode("fail")
    with pytest.raises(RuntimeError, match="semantic indexing on source failed"):
        harness.scheduler._execute_refresh(ALIAS)


def _refresh_with_outcome(
    harness: Harness, child: FakeIndexChild, outcome: str, force_reset: bool = False
) -> Any:
    if outcome == "fail":
        _fail_refresh(harness, child)
        return None
    child.set_mode(outcome)
    result = harness.scheduler._execute_refresh(ALIAS, force_reset=force_reset)
    assert result["message"] == "Refresh complete", result
    return result


def _with_recorded_jobs(harness: Harness) -> RecordingJobManager:
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    return jobs


def _age_source_files(harness: Harness) -> None:
    """No new data: every visible source file predates the published
    snapshot (the local:// change check compares mtimes)."""
    an_hour_ago = time.time() - 3600
    for path in harness.source.iterdir():
        if path.is_file() and not path.name.startswith("."):
            os.utime(path, (an_hour_ago, an_hour_ago))


def _state(metadata: Any) -> Any:
    return metadata.get_refresh_failure_backoff_state(ALIAS)


def _warnings_about_alias(caplog: pytest.LogCaptureFixture) -> list:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and ALIAS in r.getMessage()
    ]


# ---------------------------------------------------------------- (A) ----


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_failed_trigger_only_refresh_is_retried_once_its_backoff_ends(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _fail_refresh(harness, child)
    state = _state(metadata)
    assert state is not None and state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 0, "an owed retry is a failure"
    jobs = _with_recorded_jobs(harness)
    clock = _Clock(monkeypatch)

    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [], "retried inside its interval"

    clock.offset = ONE_DAY_SECONDS
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS], (
        "the failed trigger-only refresh was never retried"
    )

    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS], "the retry fired more than once (lease)"


def test_a_recorded_fatal_failure_keeps_its_failure_backoff(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(ALIAS, "chunk store unwritable")
    state = _state(metadata)

    due_at = recovery.retry_failed_trigger_only_refresh(
        metadata,
        ALIAS,
        "refresh failed",
        state["trigger_generation"],
        fatal_failure_recorded=True,
    )

    after = _state(metadata)
    assert after["consecutive_failure_count"] == 1, "counted twice"
    assert after["owed_retry_count"] == 0
    assert after["pending_trigger"] is True
    assert due_at == pytest.approx(
        state["last_failed_at"] + recovery.failure_backoff_seconds(1)
    )


# Runs real git and indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_failed_git_repo_refresh_is_left_to_its_schedule(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    child.set_mode("fail")
    with pytest.raises(RuntimeError):
        # force_reset reaches indexing with no upstream change.
        harness.scheduler._execute_refresh(ALIAS, force_reset=True)

    assert _state(metadata) is None


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
@pytest.mark.parametrize("outcome", ["fail", "partial"])
def test_a_new_data_trigger_during_an_owed_retry_is_submitted(
    tmp_path: Path, metadata: Any, child: FakeIndexChild, outcome: str
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _refresh_with_outcome(harness, child, outcome)
    assert _state(metadata)["pending_trigger"] is True
    jobs = _with_recorded_jobs(harness)

    harness.scheduler.trigger_refresh_for_repo(REPO)  # the writer's trigger

    assert jobs.submitted == [ALIAS], "new data waited for the owed retry"


def _alarming_volume(tmp_path: Path, harness: Harness, metadata: Any) -> None:
    """Story #2118: the alias spent far more than its refresh allowance in
    the last 24 h (a real stats store), so the volume alarm is raised."""
    metadata.add_repo(
        alias=REPO,
        repo_url=f"local://{REPO}",
        default_branch="main",
        clone_path=str(harness.source),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    stats = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    stats.insert_batch(
        [
            EmbeddingCallRecord(
                provider="voyageai",
                call_type="embed",
                model="voyage-code-3",
                item_count=RUNAWAY_ITEMS,
                token_count=RUNAWAY_ITEMS,
                batch_size=128,
                purpose="refresh",
                success=True,
                latency_ms=1,
                occurred_at=time.time() - 60,
                golden_repo_alias=ALIAS,
                job_id="job-earlier",
                node_id="node-1",
            )
        ]
    )
    harness.scheduler._index_spend_alarm = IndexSpendAlarm(
        stats_backend=stats,
        metadata_backend=metadata,
        stats_enabled=lambda: True,
        clock=lambda: time.time(),
        monotonic=lambda: time.time(),
    )


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
@pytest.mark.parametrize("outcome", ["fail", "partial"])
def test_retry_passes_the_volume_alarm_gate(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _refresh_with_outcome(harness, child, outcome)
    jobs = _with_recorded_jobs(harness)
    clock = _Clock(monkeypatch)
    _alarming_volume(tmp_path, harness, metadata)

    clock.offset = recovery.failure_backoff_seconds(1) + 1  # due, still alarming
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [], "the retry bypassed the volume alarm"
    state = _state(metadata)
    assert state["pending_trigger"] is True
    assert state["pending_due_at"] >= time.time() + DEFERRAL_SECONDS - 1

    clock.offset = ONE_DAY_SECONDS + 2 * DEFERRAL_SECONDS  # spend left the window
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS]


# ---------------------------------------------------------------- (B) ----


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_partial_refresh_is_reported_partial_and_logged(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")

    with caplog.at_level("WARNING"):
        result = _refresh_with_outcome(harness, child, "partial")

    assert result["success"] is True, "the indexed part was published"
    assert result.get("partial") is True, result
    assert result.get("failed_files") == len(PARTIAL_FAILED_FILES), result
    assert harness.scheduler.alias_manager.read_alias(ALIAS) != str(harness.snapshot)
    assert any(
        f"{len(PARTIAL_FAILED_FILES)} file" in m and "retried at" in m
        for m in _warnings_about_alias(caplog)
    ), _warnings_about_alias(caplog)


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_partial_refresh_is_retried_without_new_data(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _refresh_with_outcome(harness, child, "partial")
    jobs = _with_recorded_jobs(harness)
    clock = _Clock(monkeypatch)

    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [], "retried inside its interval"

    clock.offset = ONE_DAY_SECONDS
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS], "the recorded failures were never retried"


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(120)
def test_retry_of_a_partial_refresh_reindexes_without_new_data_and_resolves(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _refresh_with_outcome(harness, child, "partial")
    _age_source_files(harness)

    result = _refresh_with_outcome(harness, child, "recover")

    assert child.index_calls() == 2, "the retry did not re-run the indexer"
    assert not result.get("partial"), result
    assert _state(metadata) is None


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(120)
def test_repeated_partial_refreshes_escalate_the_owed_retry(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _refresh_with_outcome(harness, child, "partial")
    _age_source_files(harness)
    _refresh_with_outcome(harness, child, "partial")

    state = _state(metadata)
    assert state is not None and state["pending_trigger"] is True
    assert state["owed_retry_count"] == 2
    assert state["consecutive_failure_count"] == 0
    assert state["pending_due_at"] == pytest.approx(
        state["pending_marked_at"] + recovery.failure_backoff_seconds(2), abs=5
    )


# Runs real git, snapshot and indexing subprocesses.
@pytest.mark.timeout(120)
def test_a_git_repo_keeps_its_schedule_after_a_partial_publish(
    tmp_path: Path, metadata: Any, child: FakeIndexChild
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    result = _refresh_with_outcome(harness, child, "partial", force_reset=True)
    assert result.get("partial") is True
    jobs = _with_recorded_jobs(harness)
    harness.registry.update_next_refresh(ALIAS, time.time() - 5)  # due

    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS], "the owed retry deferred the git schedule"


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_a_trigger_arriving_mid_cycle_fires_promptly_after_a_partial_publish(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    real_index_source = harness.scheduler._index_source

    def _index_then_a_trigger_arrives(*args: Any, **kwargs: Any) -> None:
        real_index_source(*args, **kwargs)
        # A system trigger deferred while this cycle runs (fresh generation).
        metadata.mark_volume_alarm_deferral(ALIAS, time.time() + ONE_DAY_SECONDS)

    monkeypatch.setattr(
        harness.scheduler, "_index_source", _index_then_a_trigger_arrives
    )
    _refresh_with_outcome(harness, child, "partial")
    jobs = _with_recorded_jobs(harness)

    run_one_scheduler_iteration(harness)  # no time passes

    assert jobs.submitted == [ALIAS], "the mid-cycle trigger waited for the retry"


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_a_stale_failure_counter_is_reported_but_never_retried(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")

    with caplog.at_level("WARNING"):
        result = _refresh_with_outcome(harness, child, "stale")

    assert result.get("partial") is True
    assert result.get("failed_files") == STALE_FAILED_FILES
    assert _state(metadata) is None, "a stale counter scheduled a retry"
    assert any("not retried" in m for m in _warnings_about_alias(caplog))


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(120)
def test_unknown_recorded_failures_escalate_the_owed_retry(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _refresh_with_outcome(harness, child, "partial")
    owed = _state(metadata)["owed_retry_count"]
    _age_source_files(harness)
    meta_path = harness.source / ".code-indexer" / "metadata-voyage-ai.json"
    data = json.loads(meta_path.read_text())
    data["failed_file_paths"] = "not-a-list"  # malformed: failures unknown
    meta_path.write_text(json.dumps(data))

    with caplog.at_level("WARNING"):
        _refresh_with_outcome(harness, child, "noop")

    state = _state(metadata)
    assert state is not None, "unknown failures dropped the owed retry"
    assert state["pending_trigger"] is True
    assert state["owed_retry_count"] == owed + 1, "unknown never escalates"
    assert state["consecutive_failure_count"] == 0
    assert any("unknown" in m for m in _warnings_about_alias(caplog))


# Runs real snapshot/indexing subprocesses: seconds alone, slower under load.
@pytest.mark.timeout(90)
def test_the_partial_warning_says_when_no_retry_was_scheduled(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    # No scheduler loop runs to fire a trigger in externally managed mode.
    monkeypatch.setattr(harness.scheduler, "_is_externally_managed", lambda: True)

    with caplog.at_level("WARNING"):
        result = _refresh_with_outcome(harness, child, "partial")

    assert result.get("partial") is True
    assert _state(metadata) is None
    warnings = _warnings_about_alias(caplog)
    assert any("not retried" in m for m in warnings), warnings
    assert not any("retried at" in m for m in warnings), warnings
