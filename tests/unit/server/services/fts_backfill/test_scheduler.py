"""EVO-66445: unit tests for the FTS backfill sweep scheduler (tick + config gate)."""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.fts_backfill.scheduler import (
    FtsBackfillSweepScheduler,
)
from code_indexer.server.services.fts_backfill.sweep import FtsBackfillTickStats


class _Cfg:
    def __init__(self, **kw: Any) -> None:
        self.fts_backfill_sweep_config = type("C", (), kw)()


class _ConfigService:
    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg

    def get_config(self) -> Any:
        return self._cfg


class _BJM:
    def __init__(self, *, raise_dup: bool = False) -> None:
        self.raise_dup = raise_dup
        self.submitted: list = []

    def submit_job(self, op, fn, **kw: Any) -> str:
        if self.raise_dup:
            raise DuplicateJobError("fts_backfill_sweep", "server", "existing-job")
        self.submitted.append((op, kw))
        return "job-1"


def _scheduler(cfg: Any, bjm: Any, tick_fn: Any) -> FtsBackfillSweepScheduler:
    return FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=bjm,
        config_service=_ConfigService(cfg),
        tick_fn=tick_fn,
        subprocess_env_provider=lambda: {"ENV": "x"},
    )


def test_run_tick_uses_config_batch_size_and_reports_stats() -> None:
    seen: Dict[str, Any] = {}

    def tick_fn(grm, *, batch_size, subprocess_env):
        seen["batch_size"] = batch_size
        seen["env"] = subprocess_env
        return FtsBackfillTickStats(total_missing=5, attempted=3, rebuilt=2, failed=1)

    cfg = _Cfg(enabled=True, batch_size=3, tick_interval_minutes=15)
    sched = _scheduler(cfg, _BJM(), tick_fn)
    result = sched._run_tick()

    assert seen["batch_size"] == 3
    assert seen["env"] == {"ENV": "x"}
    assert result == {
        "total_missing": 5,
        "attempted": 3,
        "rebuilt": 2,
        "failed": 1,
        "remaining": 3,
    }


def test_read_cycle_config_fail_open_on_bad_config() -> None:
    class _Boom:
        def get_config(self):
            raise RuntimeError("db down")

    sched = FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=_BJM(),
        config_service=_Boom(),
    )
    cfg = sched._read_cycle_config()
    assert cfg["enabled"] is True  # fail-open: never silently stops
    assert cfg["batch_size"] == 10
    assert cfg["interval_minutes"] == 15


def test_trigger_now_returns_none_on_duplicate() -> None:
    cfg = _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15)
    sched = _scheduler(cfg, _BJM(raise_dup=True), lambda *a, **k: None)
    assert sched.trigger_now() is None  # another worker claimed it -> benign None


def test_trigger_now_submits_tick_job() -> None:
    cfg = _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15)
    bjm = _BJM()
    sched = _scheduler(cfg, bjm, lambda *a, **k: None)
    assert sched.trigger_now() == "job-1"
    assert bjm.submitted[0][0] == "fts_backfill_sweep"
