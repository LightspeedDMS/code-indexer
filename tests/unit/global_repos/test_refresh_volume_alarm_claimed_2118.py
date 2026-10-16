"""Story #2118 slice 1b, review P1: system refresh WORK that does not pass
through the submit-time gate is gated when it runs.

- A refresh job claimed by DistributedJobWorkerService (cluster
  reclaim/re-queue) runs ``execute_refresh_for_claimed_job`` directly.
- A job submitted through ``_submit_refresh_job`` while the alias was quiet
  can sit queued in the job manager and start after the alias began
  alarming.

For a SYSTEM job of an alarming alias: no indexing child starts, the
strike-free deferral row is persisted (the trigger fires later through the
normal path), and the job ends COMPLETED -- never failed -- with a result
naming the reason; no second job is submitted. Admin jobs always index.

Real scheduler, registry, metadata stores (SQLite always, PostgreSQL with
``TEST_POSTGRES_DSN``) and SQLite stats store; the claim boundary and the
job manager are recording doubles, and ``_index_source`` (the embedding
child) is a recorder.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.services.distributed_job_worker import (
    DistributedJobWorkerService,
)
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.index_spend_alarm import IndexSpendAlarm
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
)

RUNAWAY_ITEMS = 50_000


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


class _Claimer:
    """Claim boundary: hands out one job, records the terminal outcome."""

    def __init__(self, job: Dict[str, Any]) -> None:
        self._jobs = [job]
        self.completed: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        self.failed: List[Tuple[str, str]] = []

    def claim_next_job(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self._jobs.pop(0) if self._jobs else None

    def complete_job(self, job_id: str, result: Optional[Dict[str, Any]] = None):
        self.completed.append((job_id, result))
        return True

    def fail_job(self, job_id: str, error: str) -> bool:
        self.failed.append((job_id, error))
        return True


class _CapturingJobManager(RecordingJobManager):
    """Records submissions AND keeps each job's function, to run it later
    the way a worker would once a slot frees up."""

    def __init__(self) -> None:
        super().__init__()
        self.funcs: List[Callable[..., Any]] = []

    def submit_job(self, **kwargs: Any) -> str:
        job_id = super().submit_job(**kwargs)
        self.funcs.append(kwargs["func"])
        return job_id


def _harness(tmp_path: Path, metadata: Any) -> Tuple[Harness, Any]:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    metadata.add_repo(
        alias=REPO,
        repo_url=f"local://{REPO}",
        default_branch="main",
        clone_path=str(harness.source),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    stats = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    harness.scheduler._index_spend_alarm = IndexSpendAlarm(
        stats_backend=stats, metadata_backend=metadata, stats_enabled=lambda: True
    )
    return harness, stats


def _runaway(stats: Any) -> None:
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
            )
        ]
    )


def _claimed(username: str) -> Dict[str, Any]:
    return {
        "job_id": "job-claimed-1",
        "operation_type": "global_repo_refresh",
        "repo_alias": ALIAS,
        "username": username,
    }


def test_claimed_system_job_of_alarming_alias_does_not_index(
    tmp_path: Path, metadata: Any
) -> None:
    harness, stats = _harness(tmp_path, metadata)
    _runaway(stats)
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    claimer = _Claimer(_claimed("system"))
    worker = DistributedJobWorkerService(claimer, harness.scheduler)

    with patch.object(harness.scheduler, "_index_source") as index_child:
        worker._process_one_job()

    index_child.assert_not_called()
    assert claimer.failed == []
    ((job_id, result),) = claimer.completed
    assert job_id == "job-claimed-1"
    assert result is not None and result["skipped"] == "volume_alarm_deferred"
    assert "volume alarm" in result["message"]
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state["pending_trigger"] is True
    assert state["consecutive_failure_count"] == 0
    assert jobs.attempts == [], "a claimed job must never submit a second job"


def test_claimed_admin_job_of_alarming_alias_indexes(
    tmp_path: Path, metadata: Any
) -> None:
    harness, stats = _harness(tmp_path, metadata)
    _runaway(stats)
    claimer = _Claimer(_claimed("admin"))
    worker = DistributedJobWorkerService(claimer, harness.scheduler)

    with patch.object(harness.scheduler, "_index_source") as index_child:
        worker._process_one_job()

    index_child.assert_called_once()
    assert claimer.failed == []
    assert [job_id for job_id, _ in claimer.completed] == ["job-claimed-1"]


def test_queued_system_job_is_re_checked_when_it_runs(
    tmp_path: Path, metadata: Any
) -> None:
    harness, stats = _harness(tmp_path, metadata)
    jobs = _CapturingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    harness.scheduler.trigger_refresh_for_repo(REPO)  # quiet: submitted
    assert jobs.submitted == [ALIAS]

    _runaway(stats)  # the alias starts alarming while the job is queued
    with patch.object(harness.scheduler, "_index_source") as index_child:
        result = jobs.funcs[0](job_id="job-queued-1")

    index_child.assert_not_called()
    assert result["skipped"] == "volume_alarm_deferred"
    assert metadata.get_refresh_failure_backoff_state(ALIAS)["pending_trigger"]
