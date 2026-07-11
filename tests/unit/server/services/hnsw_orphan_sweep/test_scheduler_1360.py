"""Tests for HNSWOrphanRepairSweepScheduler (Story #1360 AC1/AC3/AC4).

Real components throughout:
  - Real hnswlib fork indexes (via HNSWIndexManager.build_index) as sweep
    targets -- no mocking of check_integrity()/repair_orphans().
  - Real HNSWOrphanSweepStateSqliteBackend over a real SQLite file.
  - Real JobTracker + its actual idx_active_job_per_repo partial unique index
    for the cross-worker single-flight dedup test (feedback_faithful_db_mocks:
    the DB gate under test must be the real driver, not an unfaithful mock).

AC1 focus: paced batch per tick, durable stable-key resume, and the
resume-correctness property under candidate-set mutation between ticks
(new items are neither silently skipped nor double-processed within the
same pass).
"""

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.server.services.job_tracker import (
    DuplicateJobError as TrackerDuplicateJobError,
    JobTracker,
)
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    HNSWOrphanSweepStateSqliteBackend,
)
from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
    HNSWOrphanRepairSweepScheduler,
)
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
)


CORPUS_DIM = 8


def _make_clean_collection(collection_path: Path, seed: int, n: int = 6) -> None:
    """Build a real, self-healed (S2) clean HNSW collection via the
    production build path -- check_integrity() will show 0 orphans."""
    collection_path.mkdir(parents=True, exist_ok=True)
    rng = np.random.RandomState(seed)
    vectors = rng.randn(n, CORPUS_DIM).astype(np.float32)
    ids = [f"vec_{i}" for i in range(n)]
    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
    manager.build_index(collection_path, vectors, ids)


class _FakeGoldenRepoManager:
    """Test double (not a Mock) -- a real, controlled stand-in for the
    minimal golden_repo_manager surface enumerate_sweep_candidates() needs."""

    def __init__(self, repos: Dict[str, Path]):
        self._repos = repos

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])

    def add_repo(self, alias: str, root: Path) -> None:
        self._repos[alias] = root


class _EmptyActivatedRepoManager:
    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return []


@pytest.fixture
def state_backend(tmp_path: Path):
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return HNSWOrphanSweepStateSqliteBackend(db_path)


class _RecordingConfigService:
    def __init__(self, *, enabled: bool = True, batch_size: int = 2):
        self.enabled = enabled
        self.batch_size = batch_size

    def get_config(self):
        cfg = self

        class _Wrapper:
            hnsw_orphan_repair_sweep_config = cfg

        return _Wrapper()


class _RealGateBackgroundJobManager:
    """submit_job() delegates straight into a REAL JobTracker instance --
    exercising the actual idx_active_job_per_repo DB-level gate, not a mock
    of it (feedback_faithful_db_mocks)."""

    def __init__(self, job_tracker: JobTracker):
        self._job_tracker = job_tracker

    def submit_job(
        self,
        operation_type: str,
        func,
        *args,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias=None,
        **kwargs,
    ) -> str:
        job_id = str(uuid.uuid4())
        try:
            self._job_tracker.register_job_if_no_conflict(
                job_id=job_id,
                operation_type=operation_type,
                username=submitter_username,
                repo_alias=repo_alias,
                is_admin=is_admin,
            )
        except TrackerDuplicateJobError as exc:
            raise DuplicateJobError(
                exc.operation_type, exc.repo_alias, exc.existing_job_id
            ) from exc
        # Execute synchronously for test determinism (no real thread pool).
        func(*args, **kwargs)
        return job_id


def _make_scheduler(
    tmp_path: Path,
    golden_repo_manager,
    state_backend,
    *,
    batch_size: int = 2,
    process_fn=None,
) -> HNSWOrphanRepairSweepScheduler:
    kwargs: Dict[str, Any] = dict(
        golden_repo_manager=golden_repo_manager,
        activated_repo_manager=_EmptyActivatedRepoManager(),
        state_backend=state_backend,
        background_job_manager=None,
        config_service=_RecordingConfigService(batch_size=batch_size),
    )
    if process_fn is not None:
        kwargs["process_fn"] = process_fn
    return HNSWOrphanRepairSweepScheduler(**kwargs)


class TestPacedBatchPerTick:
    def test_processes_at_most_batch_size_items(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        for name in ("a", "b", "c", "d"):
            _make_clean_collection(
                repo_root / ".code-indexer" / "index" / name, seed=ord(name)
            )
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})
        scheduler = _make_scheduler(tmp_path, golden, state_backend, batch_size=2)

        result = scheduler._run_tick()

        assert result["processed"] == 2
        state = state_backend.get_state()
        assert state["pass_indexes_checked"] == 2


class TestResumeAcrossCandidateSetMutation:
    def test_no_item_silently_skipped_or_double_processed(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        for name in ("a", "b", "c", "d", "e"):
            _make_clean_collection(
                repo_root / ".code-indexer" / "index" / name, seed=ord(name)
            )
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        processed_log: List[str] = []
        from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
            process_candidate as real_process_candidate,
        )

        def spy_process(candidate):
            processed_log.append(candidate.sort_key)
            return real_process_candidate(candidate)

        scheduler = _make_scheduler(
            tmp_path, golden, state_backend, batch_size=2, process_fn=spy_process
        )

        # Tick 1: processes a, b.
        scheduler._run_tick()
        assert [k.split(":")[-1].split("/")[-2] for k in processed_log] == [
            "a",
            "b",
        ]

        # Mutate the candidate set between ticks:
        #  - "f" sorts AFTER everything already known -> must be processed
        #    later in THIS pass (proves new items aren't lost).
        #  - "aa" sorts BEFORE the current cursor ("b") -> must be DEFERRED
        #    to the NEXT pass, not silently dropped nor double-processed
        #    this pass.
        _make_clean_collection(
            repo_root / ".code-indexer" / "index" / "f", seed=ord("f")
        )
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "aa", seed=999)

        # Tick 2: pending (key > cursor "...b...") = c, d, e, f -> processes c, d.
        scheduler._run_tick()
        # Tick 3: pending = e, f -> processes e, f. Nothing left beyond
        # cursor ("aa" always sorts before it) -> pass completes.
        scheduler._run_tick()

        pass1_names = [k.split(":")[-1].split("/")[-2] for k in processed_log]
        assert pass1_names == ["a", "b", "c", "d", "e", "f"]
        assert len(pass1_names) == len(set(pass1_names)), "no double-processing"

        state = state_backend.get_state()
        assert state["pass_id"] == 2, "pass completed and a new pass started"
        assert state["last_completed_key"] is None

        # Next pass picks up "aa" -- proves it was deferred, not lost.
        processed_log.clear()
        scheduler._run_tick()
        pass2_names = [k.split(":")[-1].split("/")[-2] for k in processed_log]
        assert "aa" in pass2_names


class TestPassCompletion:
    def test_complete_pass_called_when_nothing_beyond_cursor(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "only", seed=1)
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})
        scheduler = _make_scheduler(tmp_path, golden, state_backend, batch_size=5)

        scheduler._run_tick()

        state = state_backend.get_state()
        assert state["pass_id"] == 2
        assert state["last_completed_key"] is None
        assert state["last_full_pass_completed_at"] is not None


class TestFailSoftPerItem:
    def test_one_item_raising_does_not_abort_tick(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        # 3 collections with batch_size=2: the pass does NOT complete within
        # this tick, so the pass-scoped counters are still readable
        # afterward (complete_pass() resets them, so this must be verified
        # BEFORE the pass finishes).
        for name in ("a", "b", "c"):
            _make_clean_collection(
                repo_root / ".code-indexer" / "index" / name, seed=ord(name)
            )
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        calls = {"n": 0}

        def flaky_process(candidate):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return SweepOutcome.CLEAN

        scheduler = _make_scheduler(
            tmp_path, golden, state_backend, batch_size=2, process_fn=flaky_process
        )

        result = scheduler._run_tick()

        assert result["processed"] == 2
        assert result["error"] == 1
        assert result["clean"] == 1
        state = state_backend.get_state()
        assert state["pass_errors"] == 1
        assert state["pass_id"] == 1, "pass not yet complete (1 item remains)"


class TestSingleFlightAcrossWorkers:
    @pytest.fixture
    def atomic_db_path(self, tmp_path: Path) -> str:
        db = tmp_path / "test_atomic.db"
        with closing(sqlite3.connect(str(db))) as conn:
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
                progress_info TEXT,
                metadata TEXT,
                actor_username TEXT
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
        return str(db)

    def test_second_worker_tick_is_skipped_via_real_db_gate(
        self, tmp_path: Path, state_backend, atomic_db_path: str
    ) -> None:
        """Simulates a second cluster worker's tick firing while the first
        worker's tick job is still 'pending' in the REAL background_jobs
        table -- the second call must be rejected by the actual
        idx_active_job_per_repo unique index (not a mocked check), and
        trigger_now() must return None rather than raising."""
        tracker = JobTracker(atomic_db_path)

        # Simulate "worker 1" already holding the active tick job.
        tracker.register_job_if_no_conflict(
            job_id="worker-1-job",
            operation_type=HNSWOrphanRepairSweepScheduler.OPERATION_TYPE,
            username="system",
            repo_alias="server",
        )

        repo_root = tmp_path / "repo"
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "only", seed=1)
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=_EmptyActivatedRepoManager(),
            state_backend=state_backend,
            background_job_manager=_RealGateBackgroundJobManager(tracker),
            config_service=_RecordingConfigService(batch_size=5),
        )

        result = scheduler.trigger_now()

        assert result is None
        # The sweep must NOT have advanced -- worker 2's tick never ran.
        assert state_backend.get_state()["pass_indexes_checked"] == 0

    def test_trigger_now_runs_when_no_conflict(
        self, tmp_path: Path, state_backend, atomic_db_path: str
    ) -> None:
        tracker = JobTracker(atomic_db_path)
        repo_root = tmp_path / "repo"
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "only", seed=1)
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=_EmptyActivatedRepoManager(),
            state_backend=state_backend,
            background_job_manager=_RealGateBackgroundJobManager(tracker),
            config_service=_RecordingConfigService(batch_size=5),
        )

        result = scheduler.trigger_now()

        assert result is not None
        # With 1 collection and batch_size=5 the pass completes within this
        # same tick, which resets pass_indexes_checked by design -- pass_id
        # advancing to 2 is the durable evidence the tick actually ran.
        assert state_backend.get_state()["pass_id"] == 2


class TestGetStats:
    def test_get_stats_exposes_durable_state(
        self, tmp_path: Path, state_backend
    ) -> None:
        golden = _FakeGoldenRepoManager({})
        scheduler = _make_scheduler(tmp_path, golden, state_backend)

        stats = scheduler.get_stats()

        assert stats["pass_id"] == 1
        assert stats["total_orphans_repaired_lifetime"] == 0
