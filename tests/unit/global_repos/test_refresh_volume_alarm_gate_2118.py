"""Story #2118 slice 1b, acceptance (a): a local:// repo whose refresh
embedding volume exceeds the threshold.

- the next SYSTEM trigger (what the Langfuse sync sends) is deferred: no job
  is created and a strike-free pending trigger row is persisted;
- an ADMIN refresh, and a force_reset, always run;
- after the 24 h window passes (time injected), one real scheduler pass
  fires the pending trigger and the gate re-checks it.

Real scheduler, registry and metadata stores (SQLite always, PostgreSQL
with ``TEST_POSTGRES_DSN``), real SQLite stats store. Only the job
submission boundary is recorded (``RecordingJobManager``).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Tuple

import pytest

from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.index_spend_alarm import (
    DEFERRAL_SECONDS,
    IndexSpendAlarm,
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

pytestmark = pytest.mark.slow

RUNAWAY_ITEMS = 50_000


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


def _runaway(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Harness, RecordingJobManager, _Clock]:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    metadata.add_repo(
        alias=REPO,
        repo_url=f"local://{REPO}",
        default_branch="main",
        clone_path=str(harness.source),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
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
    clock = _Clock(monkeypatch)
    harness.scheduler._index_spend_alarm = IndexSpendAlarm(
        stats_backend=stats,
        metadata_backend=metadata,
        stats_enabled=lambda: True,
        clock=lambda: time.time(),
        monotonic=lambda: time.time(),
    )
    return harness, jobs, clock


def test_system_trigger_is_deferred_with_no_job_and_a_pending_row(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, _clock = _runaway(tmp_path, metadata, monkeypatch)
    before = time.time()

    with pytest.raises(RefreshDeferredError) as exc_info:
        harness.scheduler.trigger_refresh_for_repo(REPO)  # Langfuse-style

    assert jobs.submitted == []
    assert "volume alarm" in str(exc_info.value)
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state is not None and state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 0
    assert state["pending_due_at"] >= before + DEFERRAL_SECONDS


def test_admin_refresh_and_force_reset_always_run(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, _clock = _runaway(tmp_path, metadata, monkeypatch)

    harness.scheduler.trigger_refresh_for_repo(REPO, submitter_username="admin")
    harness.scheduler.trigger_refresh_for_repo(REPO, force_reset=True)

    assert jobs.submitted == [ALIAS, ALIAS]
    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None


def test_pending_trigger_fires_after_the_window_and_the_gate_re_checks(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, jobs, clock = _runaway(tmp_path, metadata, monkeypatch)
    with pytest.raises(RefreshDeferredError):
        harness.scheduler.trigger_refresh_for_repo(REPO)

    clock.offset = DEFERRAL_SECONDS + 1  # due, but still inside the window
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [], "still alarming: the fired trigger re-defers"
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state["pending_trigger"] is True
    assert state["pending_due_at"] > time.time()

    clock.offset = 24 * 3600 + 2 * DEFERRAL_SECONDS  # the spend left the window
    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [ALIAS]
