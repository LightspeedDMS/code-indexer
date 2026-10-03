"""The HNSW orphan repair sweep must be leader-gated, not race-gated.

Today the loop calls ``trigger_now()`` on every process and relies on
``DuplicateJobError`` against ``idx_active_job_per_repo`` -- mechanism M4,
"everyone submits, one wins". That is correct for idempotent work. This work is
not idempotent: a repair READS an on-disk HNSW index and REWRITES it, on a
shared NFS mount. M4 has a TOCTOU window, acknowledged in-tree at
``fts_backfill/scheduler.py``, and a duplicate that slips through it is two
processes rewriting the same index.

That window is exactly why the FTS backfill sweep was promoted M4 -> M1. This
does the same, keeping the M4 claim underneath: the gate removes the
steady-state storm, the claim still covers the failover moment when two
processes briefly both believe they are leader.

What must NOT change: ``trigger_now()`` itself stays ungated. It is the manual
admin door, and an operator asking THIS pod to sweep should get a sweep rather
than a silent no-op because another pod holds leadership.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
    CYCLE_SKIPPED_NOT_LEADER,
    HNSWOrphanRepairSweepScheduler,
)


def _config(enabled=True, window_start=0, window_end=24, interval=7):
    cfg = MagicMock()
    sweep = cfg.get_config.return_value.hnsw_orphan_repair_sweep_config
    sweep.enabled = enabled
    sweep.batch_size = 15
    sweep.tick_interval_minutes = interval
    sweep.operating_window_start_hour = window_start
    sweep.operating_window_end_hour = window_end
    return cfg


def _scheduler(leader, *, enabled=True, **kw) -> HNSWOrphanRepairSweepScheduler:
    s = HNSWOrphanRepairSweepScheduler(
        golden_repo_manager=MagicMock(),
        activated_repo_manager=MagicMock(),
        state_backend=MagicMock(),
        background_job_manager=MagicMock(),
        config_service=_config(enabled=enabled),
        now_fn=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        **({"leader_check": leader} if leader is not None else {}),
        **kw,
    )
    return s


def _run_one_cycle(sched) -> List[str]:
    """Run exactly one _loop cycle, recording trigger_now() calls."""
    calls: List[str] = []
    sched.trigger_now = lambda: calls.append("tick") or "job-1"  # type: ignore[assignment]

    # Stop after the first cycle's post-work wait begins.
    real_wait = sched._stop_event.wait

    def _wait_once(timeout=None):
        sched._stop_event.set()
        return real_wait(timeout=0)

    sched._stop_event.wait = _wait_once  # type: ignore[assignment]
    sched._loop()
    return calls


# ───────────────────────────── the gate itself ───────────────────────────────


def test_a_follower_submits_no_tick():
    assert _run_one_cycle(_scheduler(lambda: False)) == []


def test_the_leader_submits_a_tick():
    """The control: without it, the test above passes on a loop that never
    submits under any conditions."""
    assert _run_one_cycle(_scheduler(lambda: True)) == ["tick"]


def test_no_leader_check_configured_behaves_exactly_as_before():
    """Solo/sqlite has no election. The default must be "I am the leader", so a
    single-node deployment is byte-for-byte unchanged."""
    assert _run_one_cycle(_scheduler(None)) == ["tick"]


def test_a_raising_leader_check_fails_open():
    """A glitch in the election must not silently stop the fleet's only
    mechanism for repairing corrupt indexes. Same direction as every other
    gate in this tree: fail-open can only ever be as bad as the pre-gate
    behaviour, where all 12 processes submitted."""

    def _boom() -> bool:
        raise RuntimeError("election backend down")

    assert _run_one_cycle(_scheduler(_boom)) == ["tick"]


# ──────────────────────────── liveness (Bug #1864) ───────────────────────────


def test_a_follower_still_heartbeats():
    """Bug #1864's whole point: a cycle that deliberately submits nothing must
    be distinguishable from a loop thread that died. Without this, 11 of 12
    pods would report as dead to the admin endpoint."""
    s = _scheduler(lambda: False)
    _run_one_cycle(s)
    assert s.get_liveness()["last_loop_cycle_at"] is not None


def test_a_follower_reports_why_it_skipped():
    s = _scheduler(lambda: False)
    _run_one_cycle(s)
    assert s.get_liveness()["last_cycle_skipped_reason"] == CYCLE_SKIPPED_NOT_LEADER


def test_the_leader_reports_no_skip_reason():
    s = _scheduler(lambda: True)
    _run_one_cycle(s)
    assert s.get_liveness()["last_cycle_skipped_reason"] is None


def test_not_leader_is_distinct_from_the_other_skip_reasons():
    """Three different reasons a cycle is quiet; an operator needs to tell them
    apart. "disabled" is a decision, "outside window" is a schedule, "not
    leader" is normal cluster behaviour on 11 of 12 processes."""
    from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
        CYCLE_SKIPPED_DISABLED,
        CYCLE_SKIPPED_OUTSIDE_WINDOW,
    )

    assert (
        len(
            {
                CYCLE_SKIPPED_NOT_LEADER,
                CYCLE_SKIPPED_DISABLED,
                CYCLE_SKIPPED_OUTSIDE_WINDOW,
            }
        )
        == 3
    )


def test_disabled_still_outranks_not_leader():
    """A disabled sweep reports "disabled" on every pod, leader or not --
    otherwise the leader says "disabled" and 11 pods say "not leader", and the
    operator who just switched it off sees a confusing fleet."""
    s = _scheduler(lambda: False, enabled=False)
    _run_one_cycle(s)
    from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
        CYCLE_SKIPPED_DISABLED,
    )

    assert s.get_liveness()["last_cycle_skipped_reason"] == CYCLE_SKIPPED_DISABLED


# ──────────────────────── the manual door stays open ─────────────────────────


def test_trigger_now_is_not_leader_gated():
    """The admin router calls this. An operator asking THIS pod to sweep must
    get a sweep, not a silent no-op because another pod holds leadership."""
    s = _scheduler(lambda: False)
    s._background_job_manager.submit_job.return_value = "job-9"
    assert s.trigger_now() == "job-9"
    s._background_job_manager.submit_job.assert_called_once()


# ───────────────────────── failover is picked up fast ────────────────────────


class TestFollowerWaitCadence:
    """A follower that waited the full interval would leave the fleet without a
    sweep for minutes after a failover -- and this sweep's interval is in
    MINUTES and gated by an operating-hours window, so one missed cycle can mean
    waiting until the window reopens tomorrow.
    """

    @staticmethod
    def _wait(**kw) -> int:
        args = {
            "enabled": True,
            "within_window": True,
            "is_leader": True,
            "interval_minutes": 7,
        }
        args.update(kw)
        return HNSWOrphanRepairSweepScheduler._wait_seconds_for(**args)

    def test_a_follower_waits_the_short_leader_poll(self):
        from code_indexer.server.services.hnsw_orphan_sweep import scheduler as mod

        assert self._wait(is_leader=False) == mod._LEADER_POLL_SECONDS

    def test_the_leader_waits_the_full_tick_interval(self):
        assert self._wait(is_leader=True) == 7 * 60

    def test_the_follower_poll_is_shorter_than_a_tick(self):
        """The property that matters, independent of the exact numbers."""
        assert self._wait(is_leader=False) < self._wait(is_leader=True)

    def test_disabled_and_out_of_window_keep_their_own_cadence(self):
        from code_indexer.server.services.hnsw_orphan_sweep import scheduler as mod

        assert self._wait(enabled=False) == mod._DISABLED_POLL_SECONDS
        assert self._wait(within_window=False) == mod._DISABLED_POLL_SECONDS

    def test_a_disabled_follower_does_not_get_the_leader_poll(self):
        """Disabled outranks leadership for cadence too -- a switched-off sweep
        should not wake every 30s on 11 pods to ask who the leader is."""
        from code_indexer.server.services.hnsw_orphan_sweep import scheduler as mod

        assert self._wait(enabled=False, is_leader=False) == mod._DISABLED_POLL_SECONDS


def test_the_m4_claim_is_still_in_place():
    """Belt and braces: the gate removes the steady-state storm, the M4 claim
    still covers the failover moment when two processes briefly both believe
    they are leader. Dropping either one is a regression.

    Asserted by raising the real DuplicateJobError through submit_job, not by
    grepping trigger_now's source. The source form passed a mutation that
    changed ``except DuplicateJobError`` to ``except RuntimeError`` -- because
    the method's DOCSTRING also names the exception, so the substring was still
    there while the handler was gone.
    """
    from code_indexer.server.repositories.background_jobs import DuplicateJobError

    s = _scheduler(lambda: True)
    s._background_job_manager.submit_job.side_effect = DuplicateJobError(
        "hnsw_orphan_repair_sweep_tick", "server", "other-pod-job"
    )
    assert s.trigger_now() is None


def test_a_non_duplicate_submit_error_is_not_swallowed():
    """Only the duplicate is benign. Anything else must surface, or a broken
    tick submission looks identical to a tick another pod already claimed."""
    s = _scheduler(lambda: True)
    s._background_job_manager.submit_job.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        s.trigger_now()


class TestLifespanWiring:
    """The gate must actually reach the scheduler.

    Every test above injects ``leader_check`` directly, so all of them pass
    while the composition root never wires it -- a scheduler defaulting to
    "always leader" on all 12 processes, which is exactly the behaviour this
    change removes.
    """

    @staticmethod
    def _construct(app_state):
        from code_indexer.server.startup.lifespan import (
            _construct_hnsw_orphan_repair_sweep_scheduler,
        )

        grm = MagicMock()
        grm.activated_repo_manager = MagicMock()
        return _construct_hnsw_orphan_repair_sweep_scheduler(
            grm, MagicMock(), MagicMock(), app_state=app_state
        )

    def test_a_follower_pod_builds_a_follower_scheduler(self):
        state = SimpleNamespace(
            scip_reconcile_leader_gate_ready=True,
            leader_election=SimpleNamespace(is_leader=False),
        )
        assert self._construct(state)._is_leader() is False

    def test_a_leader_pod_builds_a_leader_scheduler(self):
        state = SimpleNamespace(
            scip_reconcile_leader_gate_ready=True,
            leader_election=SimpleNamespace(is_leader=True),
        )
        assert self._construct(state)._is_leader() is True

    def test_solo_wiring_without_an_app_state_still_sweeps(self):
        """sqlite/solo has no election and no app.state to consult."""
        assert self._construct(None)._is_leader() is True

    def test_the_startup_helper_passes_app_state_through(self):
        """M12: the helper above can be perfectly wired while its only caller
        forgets the argument, leaving every pod a leader."""
        from code_indexer.server.startup import lifespan as mod

        app = SimpleNamespace(
            state=SimpleNamespace(
                scip_reconcile_leader_gate_ready=True,
                leader_election=SimpleNamespace(is_leader=False),
            )
        )
        grm = MagicMock()
        grm.activated_repo_manager = MagicMock()
        scheduler = mod._start_hnsw_orphan_repair_sweep_scheduler(
            app,
            golden_repo_manager=grm,
            backend_registry=MagicMock(),
            background_job_manager=MagicMock(),
        )
        try:
            assert scheduler is not None, "scheduler failed to start"
            assert scheduler._is_leader() is False
        finally:
            if scheduler is not None:
                scheduler.stop()
