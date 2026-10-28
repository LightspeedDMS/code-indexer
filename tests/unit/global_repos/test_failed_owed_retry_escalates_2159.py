"""Bug #2159: a refresh that FAILS while it covered a pending owed-retry
trigger (Bug #2125) escalates that owed retry -- one more owed count, the
interval from the shared plan (300 s doubling to the 21600 s cap), a fresh
trigger generation -- instead of leaving the leased trigger to re-fire at a
fixed interval forever. Git and non-git repositories alike.

Real scheduler loop, registry, snapshot manager and metadata store (SQLite
always, PostgreSQL with ``TEST_POSTGRES_DSN``). The indexing child is a real
process whose ``index`` outcome is scripted (``fake_index_child``); only the
job submission boundary is recorded (``RecordingJobManager``).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.global_repos import refresh_failure_recovery as recovery
from tests.utils.fake_index_child import FakeIndexChild, install_fake_index_child
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

FAR_FUTURE_SECONDS = 30 * 24 * 3600
#: Slack for the wall-clock time spent by the real refresh subprocesses.
DUE_TOLERANCE_SECONDS = 30


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


def _state(metadata: Any) -> Any:
    return metadata.get_refresh_failure_backoff_state(ALIAS)


def _fire_owed_trigger(harness: Harness, clock: _Clock) -> RecordingJobManager:
    """Move time past the pending trigger's due time and run one real
    scheduler pass: the trigger is leased and its refresh submitted."""
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    # Keep the git schedule out of the way: only the owed trigger fires.
    harness.registry.update_next_refresh(ALIAS, time.time() + FAR_FUTURE_SECONDS)
    clock.offset += _state(harness.metadata)["pending_due_at"] - time.time() + 1
    run_one_scheduler_iteration(harness)
    assert jobs.submitted == [ALIAS], "the owed retry did not fire"
    harness.scheduler.background_job_manager = None
    return jobs


def test_only_the_covered_pending_owed_retry_counts(metadata: Any) -> None:
    """A failed cycle escalates only the owed retry it served: no row, an
    unknown generation, a strike-free volume-alarm deferral (no owed count)
    and a cycle that covered an older generation than the row's current
    trigger are left alone."""
    assert not recovery.covers_pending_owed_retry(metadata, ALIAS, 0)

    metadata.mark_volume_alarm_deferral(ALIAS, time.time() + 60)
    alarm_generation = _state(metadata)["trigger_generation"]
    assert not recovery.covers_pending_owed_retry(metadata, ALIAS, alarm_generation)

    recovery.schedule_owed_retry(metadata, ALIAS, "1 file(s) still owed", None)
    owed_generation = _state(metadata)["trigger_generation"]
    assert recovery.covers_pending_owed_retry(metadata, ALIAS, owed_generation)
    assert not recovery.covers_pending_owed_retry(metadata, ALIAS, None)
    assert not recovery.covers_pending_owed_retry(metadata, ALIAS, alarm_generation), (
        "a cycle that covered an older generation escalated the row's newer trigger"
    )


# Runs real git, snapshot and indexing subprocesses: slower under load.
@pytest.mark.slow
@pytest.mark.timeout(180)
@pytest.mark.parametrize("git_remote", [True, False], ids=["git", "local"])
def test_a_failed_owed_retry_escalates_instead_of_refiring_at_its_lease(
    tmp_path: Path,
    metadata: Any,
    child: FakeIndexChild,
    monkeypatch: pytest.MonkeyPatch,
    git_remote: bool,
) -> None:
    harness = build_harness(
        tmp_path, metadata, snapshot_mode="clean", git_remote=git_remote
    )
    child.set_mode("partial")
    result = harness.scheduler._execute_refresh(ALIAS, force_reset=git_remote)
    assert result.get("partial") is True, result
    owed = _state(metadata)
    assert owed["owed_retry_count"] == 1 and owed["pending_trigger"] is True
    clock = _Clock(monkeypatch)
    child.set_mode("fail")

    for expected_count in (2, 3):
        before = _state(metadata)
        _fire_owed_trigger(harness, clock)
        with pytest.raises(RuntimeError, match="semantic indexing on source failed"):
            harness.scheduler._execute_refresh(ALIAS)

        after = _state(metadata)
        assert after is not None and after["pending_trigger"] is True
        assert after["owed_retry_count"] == expected_count, (
            "the failed owed retry did not escalate: its leased trigger "
            "re-fires at a fixed interval"
        )
        assert after["consecutive_failure_count"] == 0, "an owed retry is a failure"
        assert after["trigger_generation"] != before["trigger_generation"]
        assert after["pending_due_at"] == pytest.approx(
            time.time() + recovery.failure_backoff_seconds(expected_count),
            abs=DUE_TOLERANCE_SECONDS,
        )
