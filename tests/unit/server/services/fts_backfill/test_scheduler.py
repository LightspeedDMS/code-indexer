"""EVO-66445: unit tests for the FTS backfill sweep scheduler (tick + config gate)."""

from __future__ import annotations

import time
from typing import Any, Dict

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from types import SimpleNamespace

from code_indexer.server.services.fts_backfill.scheduler import (
    FtsBackfillSweepScheduler,
    resolve_sweep_leader,
)
from code_indexer.server.services.golden_index_backfill import BackfillCounts


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
    )


def test_run_tick_enqueues_via_cycle_and_reports_counts() -> None:
    """The tick delegates to the enqueue cycle with (grm, bjm, batch_size, cooldown)
    and surfaces the engine's BackfillCounts as a dict (it builds nothing itself)."""
    seen: Dict[str, Any] = {}
    grm, bjm = object(), _BJM()

    def tick_fn(grm_arg, bjm_arg, *, batch_size, cooldown_seconds):
        seen["grm"] = grm_arg
        seen["bjm"] = bjm_arg
        seen["batch_size"] = batch_size
        seen["cooldown_seconds"] = cooldown_seconds
        return BackfillCounts(scanned=5, present=1, not_eligible=1, enqueued=3)

    cfg = _Cfg(
        enabled=True, batch_size=3, tick_interval_minutes=15, build_timeout_seconds=900
    )
    sched = FtsBackfillSweepScheduler(
        golden_repo_manager=grm,
        background_job_manager=bjm,
        config_service=_ConfigService(cfg),
        tick_fn=tick_fn,
    )
    result = sched._run_tick()

    assert seen["grm"] is grm
    assert seen["bjm"] is bjm  # the cycle needs the BJM to enqueue per-repo build jobs
    assert seen["batch_size"] == 3
    assert seen["cooldown_seconds"] > 0  # default per-repo attempt cooldown
    assert result == {
        "scanned": 5,
        "present": 1,
        "not_eligible": 1,
        "cooldown_skipped": 0,
        "enqueued": 3,
        "errors": 0,
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
    assert cfg["build_timeout_seconds"] == 1800


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


# ── EVO-66445 follow-up: leader-gating (single sweep across the cluster) ──


def test_is_leader_defaults_true_for_single_node() -> None:
    """No injected leader_check (single-node/sqlite, and existing behaviour) -> runs."""
    sched = _scheduler(
        _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15),
        _BJM(),
        lambda *a, **k: None,
    )
    assert sched._is_leader() is True


def test_is_leader_reflects_injected_check() -> None:
    cfg = _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15)
    follower = FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=_BJM(),
        config_service=_ConfigService(cfg),
        leader_check=lambda: False,
    )
    leader = FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=_BJM(),
        config_service=_ConfigService(cfg),
        leader_check=lambda: True,
    )
    assert follower._is_leader() is False
    assert leader._is_leader() is True


def test_is_leader_fail_open_on_check_error() -> None:
    """A leader-check glitch must NOT silently stop the sweep -> fail-open (True)."""

    def boom() -> bool:
        raise RuntimeError("election backend down")

    cfg = _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15)
    sched = FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=_BJM(),
        config_service=_ConfigService(cfg),
        leader_check=boom,
    )
    assert sched._is_leader() is True


def test_loop_does_not_submit_when_not_leader() -> None:
    """A follower node polls leadership but submits NO tick (the whole point)."""
    cfg = _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15)
    bjm = _BJM()
    sched = FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=bjm,
        config_service=_ConfigService(cfg),
        tick_fn=lambda *a, **k: None,
        leader_check=lambda: False,
    )
    sched.start()
    time.sleep(0.3)  # ample for the loop's first iteration (skip + short poll)
    sched.stop()
    assert bjm.submitted == []  # follower never submitted a tick


def test_loop_submits_when_leader() -> None:
    """The leader node submits a tick on its first iteration."""
    cfg = _Cfg(enabled=True, batch_size=10, tick_interval_minutes=15)
    bjm = _BJM()
    sched = FtsBackfillSweepScheduler(
        golden_repo_manager=object(),
        background_job_manager=bjm,
        config_service=_ConfigService(cfg),
        tick_fn=lambda *a, **k: None,
        leader_check=lambda: True,
    )
    sched.start()
    time.sleep(0.3)
    sched.stop()
    assert len(bjm.submitted) >= 1
    assert bjm.submitted[0][0] == "fts_backfill_sweep"


# ── EVO-66445: the ACTUAL production wiring (resolve_sweep_leader on app.state) ──


def test_resolve_sweep_leader_holds_until_gate_ready() -> None:
    """Cluster startup: leader-election wiring not yet ready -> do NOT run (avoid the
    all-pods-run window before leadership is established)."""
    st = SimpleNamespace(scip_reconcile_leader_gate_ready=False, leader_election=None)
    assert resolve_sweep_leader(st) is False


def test_resolve_sweep_leader_defers_to_election_once_ready() -> None:
    leader = SimpleNamespace(
        scip_reconcile_leader_gate_ready=True,
        leader_election=SimpleNamespace(is_leader=True),
    )
    follower = SimpleNamespace(
        scip_reconcile_leader_gate_ready=True,
        leader_election=SimpleNamespace(is_leader=False),
    )
    assert resolve_sweep_leader(leader) is True
    assert resolve_sweep_leader(follower) is False


def test_resolve_sweep_leader_single_node_no_election_runs() -> None:
    """sqlite/single-node: no leader_election object -> always leader (unchanged)."""
    st = SimpleNamespace(scip_reconcile_leader_gate_ready=True, leader_election=None)
    assert resolve_sweep_leader(st) is True


def test_resolve_sweep_leader_defaults_run_when_attrs_absent() -> None:
    """A bare app.state with neither attr (e.g. sqlite before any cluster wiring) must
    default to running, never silently stall the sweep."""
    assert resolve_sweep_leader(SimpleNamespace()) is True
