"""Bug #1864: HNSWOrphanRepairSweepScheduler must expose a per-process
liveness snapshot so the admin stats endpoint can tell a wedged tick from a
completed one, and a dead scheduler from either.

Real components: a real ``HNSWOrphanSweepStateSqliteBackend`` over a real
SQLite file, real ``SweepCandidate`` enumeration over a real on-disk
collection directory, and the real ``_run_tick`` path. Only the clock
(``now_fn``) and the per-item processor (``process_fn``) are controlled --
both are injection seams the scheduler already exposes.

Cluster correctness (CLAUDE.md "Cluster-Aware State"): this snapshot is
per-PROCESS RAM by design -- "is the scheduler running in THIS process" is
not fleet state and must never be stored in, or read from, the shared
backend. The scope marker on the snapshot is what keeps that honest.

Typing note: ``Dict[str, Any]`` appears only for JSON-shaped payloads -- the
durable sweep state row and the liveness snapshot. Their values are
genuinely heterogeneous (``int``/``str``/``bool``/``None``) and cross a SQL
or JSON boundary, so no narrower static type exists without a TypedDict that
would have to be kept in lockstep with two storage backends.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

from code_indexer.server.services.hnsw_orphan_sweep.discovery import SweepCandidate
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import SweepOutcome
from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
    HNSWOrphanRepairSweepScheduler,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    HNSWOrphanSweepStateSqliteBackend,
)


_T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)

ProcessFn = Callable[[SweepCandidate], SweepOutcome]


class _StepClock:
    """Deterministic clock that advances a fixed step on every read, so
    start/finish stamps are distinct and orderable without sleeping."""

    def __init__(self, start: datetime, step_seconds: int = 1) -> None:
        self._now = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self._now
        self._now = self._now + self._step
        return current


class _FakeGoldenRepoManager:
    def __init__(self, repos: Dict[str, Path]) -> None:
        self._repos = repos

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])


class _EmptyActivatedRepoManager:
    def list_all_activated_repositories(self) -> List[Dict[str, str]]:
        return []


class _FakeConfigService:
    """Minimal stand-in for ConfigService exposing only the sweep config the
    scheduler reads."""

    def __init__(self, batch_size: int) -> None:
        self._batch_size = batch_size

    def get_config(self) -> SimpleNamespace:
        return SimpleNamespace(
            hnsw_orphan_repair_sweep_config=SimpleNamespace(
                enabled=True,
                batch_size=self._batch_size,
                tick_interval_minutes=7,
                operating_hours_start_utc=0,
                operating_hours_end_utc=0,
            )
        )


class _ExplodingStateBackend:
    """State backend whose very first read raises, to drive a tick that fails
    out of ``_run_tick`` entirely."""

    def get_state(self) -> Dict[str, Any]:
        raise RuntimeError("state backend unavailable")


@pytest.fixture
def state_backend(tmp_path: Path) -> HNSWOrphanSweepStateSqliteBackend:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return HNSWOrphanSweepStateSqliteBackend(db_path)


def _make_scheduler(
    state_backend: HNSWOrphanSweepStateSqliteBackend,
    *,
    process_fn: ProcessFn,
    candidates_root: Optional[Path] = None,
    clock: Optional[_StepClock] = None,
) -> HNSWOrphanRepairSweepScheduler:
    """Build a scheduler with no background_job_manager -- these tests call
    ``_run_tick()`` directly, which the constructor explicitly supports."""
    repos: Dict[str, Path] = {}
    if candidates_root is not None:
        repos["alpha"] = candidates_root
    return HNSWOrphanRepairSweepScheduler(
        golden_repo_manager=_FakeGoldenRepoManager(repos),
        activated_repo_manager=_EmptyActivatedRepoManager(),
        state_backend=state_backend,
        background_job_manager=None,
        config_service=_FakeConfigService(batch_size=5),
        process_fn=process_fn,
        now_fn=clock or _StepClock(_T0),
    )


def _make_candidate_tree(root: Path) -> None:
    """One real on-disk collection directory so enumerate_sweep_candidates
    yields exactly one candidate."""
    collection = root / ".code-indexer" / "index" / "c1"
    collection.mkdir(parents=True, exist_ok=True)
    (collection / "hnsw_index.bin").write_bytes(b"\x00")
    (collection / "collection_meta.json").write_text("{}")


def _clean(candidate: SweepCandidate) -> SweepOutcome:
    return SweepOutcome.CLEAN


class TestLivenessSnapshotShape:
    def test_reports_not_running_before_start(
        self, state_backend: HNSWOrphanSweepStateSqliteBackend
    ) -> None:
        scheduler = _make_scheduler(state_backend, process_fn=_clean)

        liveness = scheduler.get_liveness()

        assert liveness["scheduler_running"] is False
        assert liveness["scope"] == "local_process"
        assert liveness["ticks_started"] == 0
        assert liveness["ticks_completed"] == 0
        assert liveness["tick_in_progress"] is False
        assert liveness["last_tick_at"] is None
        assert liveness["last_tick_completed_at"] is None
        assert liveness["last_tick_error"] is None


class TestTickLiveness:
    def test_completed_tick_stamps_start_and_completion(
        self, state_backend: HNSWOrphanSweepStateSqliteBackend, tmp_path: Path
    ) -> None:
        root = tmp_path / "alpha"
        _make_candidate_tree(root)
        scheduler = _make_scheduler(
            state_backend, process_fn=_clean, candidates_root=root
        )

        scheduler._run_tick()

        liveness = scheduler.get_liveness()
        assert liveness["ticks_started"] == 1
        assert liveness["ticks_completed"] == 1
        assert liveness["tick_in_progress"] is False
        assert liveness["last_tick_at"] is not None
        assert liveness["last_tick_completed_at"] is not None
        assert liveness["last_tick_at"] < liveness["last_tick_completed_at"]
        assert liveness["last_tick_error"] is None

    def test_tick_in_progress_is_visible_while_the_tick_is_executing(
        self, state_backend: HNSWOrphanSweepStateSqliteBackend, tmp_path: Path
    ) -> None:
        """THE wedge discriminator: while a tick is mid-flight the snapshot
        must say so. Without this, a tick that never returns is
        indistinguishable from one that finished cleanly -- exactly the
        ambiguity that let a two-month-dead sweep look normal."""
        root = tmp_path / "alpha"
        _make_candidate_tree(root)
        observed: Dict[str, Any] = {}

        def _observe_mid_tick(candidate: SweepCandidate) -> SweepOutcome:
            observed.update(scheduler.get_liveness())
            return SweepOutcome.CLEAN

        scheduler = _make_scheduler(
            state_backend, process_fn=_observe_mid_tick, candidates_root=root
        )

        scheduler._run_tick()

        assert observed["tick_in_progress"] is True
        assert observed["ticks_started"] == 1
        assert observed["ticks_completed"] == 0
        # ...and it is back to False once the tick returns.
        assert scheduler.get_liveness()["tick_in_progress"] is False

    def test_failing_tick_records_the_error_and_is_not_left_in_progress(
        self, state_backend: HNSWOrphanSweepStateSqliteBackend, tmp_path: Path
    ) -> None:
        """A tick that raises out of ``_run_tick`` (e.g. Bug #1415's KeyError
        era) must be recorded, not silently forgotten -- and must not leave
        the snapshot permanently claiming a tick is in flight."""
        root = tmp_path / "alpha"
        _make_candidate_tree(root)
        scheduler = _make_scheduler(
            state_backend, process_fn=_clean, candidates_root=root
        )
        scheduler._state_backend = _ExplodingStateBackend()

        with pytest.raises(RuntimeError):
            scheduler._run_tick()

        liveness = scheduler.get_liveness()
        assert liveness["tick_in_progress"] is False
        assert liveness["ticks_started"] == 1
        assert liveness["ticks_completed"] == 1
        assert "state backend unavailable" in liveness["last_tick_error"]


class TestTickResultPreservation:
    def test_tick_result_is_returned_unchanged(
        self, state_backend: HNSWOrphanSweepStateSqliteBackend, tmp_path: Path
    ) -> None:
        """Liveness instrumentation is a thin wrapper -- the tick's own
        per-outcome counts contract is untouched."""
        root = tmp_path / "alpha"
        _make_candidate_tree(root)
        scheduler = _make_scheduler(
            state_backend, process_fn=_clean, candidates_root=root
        )

        result = scheduler._run_tick()

        assert result["processed"] == 1
        assert result[SweepOutcome.CLEAN.value] == 1


class TestAbsentSchedulerLivenessTemplate:
    def test_absent_template_has_exactly_the_same_keys_as_a_real_snapshot(
        self, state_backend: HNSWOrphanSweepStateSqliteBackend
    ) -> None:
        """The stats endpoint renders the same key set whether or not a
        scheduler object exists, so a monitor never has to branch on shape."""
        from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
            absent_local_scheduler_liveness,
        )

        scheduler = _make_scheduler(state_backend, process_fn=_clean)

        assert set(absent_local_scheduler_liveness()) == set(scheduler.get_liveness())

    def test_absent_template_reports_a_dead_scheduler(self) -> None:
        from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
            absent_local_scheduler_liveness,
        )

        template = absent_local_scheduler_liveness()

        assert template["scheduler_running"] is False
        assert template["scope"] == "local_process"

    def test_absent_template_is_a_fresh_copy_each_call(self) -> None:
        """It must never be a shared mutable module-level dict the endpoint
        could scribble the per-request startup_error into."""
        from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
            absent_local_scheduler_liveness,
        )

        first = absent_local_scheduler_liveness()
        first["scheduler_running"] = True

        assert absent_local_scheduler_liveness()["scheduler_running"] is False
