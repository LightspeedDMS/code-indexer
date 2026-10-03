"""Background refreshes must yield to serving traffic, not outrun it.

Observed in tools-test on a freshly rolled pod: the refresh scheduler spawned
`cidx index` subprocesses continuously while the SAME pod answered
`xray_search`, `regex_search` and `list_files` with
``{"error_code": "memory_pressure", "retry_after_seconds": 30}``. The pod sat
at 2004m of its 2000m CPU limit.

That is a priority inversion. `check_query_admission()` gates USER requests
(handlers/git_read.py, repos.py, files.py all call it) but nothing consults it
before starting maintenance, so the background work that caused the pressure
kept running while the traffic it starved got rejected.

Leader-gating the sweep was considered and rejected: refresh work is
deliberately distributed (`_scheduler_loop`'s per-repo next_refresh jitter,
Story #284, plus `execute_refresh_for_claimed_job` and a per-alias DB write
lock), so funnelling it onto one node would slow recovery ~Nx without fixing
the inversion anywhere else.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.server.services.query_admission_gate import AdmissionDecision


def _scheduler_with_due_repos(monkeypatch):
    """A scheduler whose registry reports one due git repo."""
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    sched = RefreshScheduler.__new__(RefreshScheduler)
    sched._running = True
    sched.registry = MagicMock()
    sched.registry.list_due_repos.return_value = [
        {"alias_name": "repo-global", "repo_url": "https://example.invalid/r.git"}
    ]
    sched._submit_refresh_job = MagicMock(return_value="job-1")
    return sched


class TestRefreshDefersUnderMemoryPressure:
    def test_no_refresh_is_submitted_while_admission_is_denied(self, monkeypatch):
        sched = _scheduler_with_due_repos(monkeypatch)
        denied = AdmissionDecision(allowed=False, retry_after_seconds=30)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            return_value=denied,
        ):
            assert sched._refresh_admission_allowed() is False
        sched._submit_refresh_job.assert_not_called()

    def test_refresh_proceeds_when_admission_is_allowed(self, monkeypatch):
        sched = _scheduler_with_due_repos(monkeypatch)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            return_value=AdmissionDecision(allowed=True),
        ):
            assert sched._refresh_admission_allowed() is True

    def test_a_broken_gate_does_not_wedge_maintenance(self, monkeypatch):
        """Fail OPEN. The gate is a courtesy to serving traffic, not a
        correctness barrier -- if it raises, refreshes must still happen, or a
        bug in the governor silently stops every repo from ever updating."""
        sched = _scheduler_with_due_repos(monkeypatch)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            side_effect=RuntimeError("governor exploded"),
        ):
            assert sched._refresh_admission_allowed() is True


def _loop_scheduler(due=1):
    """A RefreshScheduler wired just enough to run ONE _scheduler_loop pass.

    Everything the iteration touches is stubbed, and ``_stop_event.wait``
    clears ``_running`` so the loop exits after a single pass instead of
    spinning. This is the only way to test the CALL SITE: the helper can be
    perfectly correct while nothing calls it.
    """
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    sched = RefreshScheduler.__new__(RefreshScheduler)
    sched._running = True
    sched.cleanup_stale_write_mode_markers = MagicMock()
    sched.get_refresh_interval = MagicMock(return_value=3600)
    sched._assign_initial_spread = MagicMock()
    sched._get_refresh_budget = MagicMock(return_value=5)
    sched._count_active_refresh_jobs = MagicMock(return_value=0)
    sched._calculate_jitter = MagicMock(return_value=0)
    sched._calculate_poll_interval = MagicMock(return_value=0)
    sched._submit_refresh_job = MagicMock(return_value="job-1")

    repos = [
        {
            "alias_name": f"repo{i}-global",
            "repo_url": "https://example.invalid/r.git",
            "next_refresh": 1.0,
        }
        for i in range(due)
    ]
    sched.registry = MagicMock()
    sched.registry.list_global_repos.return_value = repos
    sched.registry.list_due_repos.return_value = repos

    sched._db_throttle = MagicMock()
    sched._db_throttle.next_wait_seconds.return_value = 0

    def _stop_after_one_pass(timeout=None):
        sched._running = False
        return False

    sched._stop_event = MagicMock()
    sched._stop_event.wait.side_effect = _stop_after_one_pass
    return sched


def _run_one_pass(sched, decision):
    with patch(
        "code_indexer.global_repos.refresh_scheduler.check_query_admission",
        return_value=decision,
    ):
        sched._scheduler_loop()


class TestTheLoopActuallyDefers:
    """Drives a real loop iteration.

    The previous version of this class asserted on ``inspect.getsource``
    substrings. Review showed two of those could not fail: the
    "next_refresh is not advanced" check passed because the log message
    itself contains the word "unadvanced", and the "no refresh submitted"
    test never invoked the loop at all, so its ``assert_not_called`` held
    trivially. Both would have stayed green with the call site deleted.
    """

    def test_a_denied_admission_submits_nothing(self):
        sched = _loop_scheduler(due=3)
        _run_one_pass(sched, AdmissionDecision(allowed=False, retry_after_seconds=30))
        sched._submit_refresh_job.assert_not_called()

    def test_a_denied_admission_leaves_next_refresh_unadvanced(self):
        """Deferring must be free.

        Advancing next_refresh while skipping the work would make a pod under
        sustained pressure silently drop whole refresh cycles rather than retry
        them -- the repos would go quiet for an interval each time.
        """
        sched = _loop_scheduler(due=3)
        _run_one_pass(sched, AdmissionDecision(allowed=False, retry_after_seconds=30))
        sched.registry.update_next_refresh.assert_not_called()

    def test_an_allowed_admission_submits_and_advances(self):
        """The control. Without it the two tests above pass on a loop that
        never submits anything under any conditions."""
        sched = _loop_scheduler(due=3)
        _run_one_pass(sched, AdmissionDecision(allowed=True))
        assert sched._submit_refresh_job.call_count == 3
        assert sched.registry.update_next_refresh.call_count == 3

    def test_a_raising_gate_still_refreshes(self):
        """Fail-open, exercised through the loop rather than the helper."""
        sched = _loop_scheduler(due=2)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            side_effect=RuntimeError("governor exploded"),
        ):
            sched._scheduler_loop()
        assert sched._submit_refresh_job.call_count == 2

    def test_the_gate_is_consulted_once_per_batch_not_per_repo(self):
        """The decision is pod-wide; re-checking mid-batch would abandon a
        partially-submitted cycle for no benefit."""
        sched = _loop_scheduler(due=4)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            return_value=AdmissionDecision(allowed=True),
        ) as gate:
            sched._scheduler_loop()
        assert gate.call_count == 1

    def test_an_empty_due_list_does_not_consult_the_gate(self):
        """No work to defer means no reason to ask -- and no reason to log."""
        sched = _loop_scheduler(due=0)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            return_value=AdmissionDecision(allowed=True),
        ) as gate:
            sched._scheduler_loop()
        gate.assert_not_called()


class TestDeferralDoesNotPolluteTheQueryMetric:
    """``query_admissions_denied`` is published in MemoryGovernor.get_snapshot()
    and means "live callers we turned away". A deferred refresh poll is not a
    turned-away query, and this loop would bump it every cycle under sustained
    pressure -- reporting a rejection storm no user ever experienced.
    """

    def test_the_refresh_gate_does_not_count_its_denial(self):
        sched = _loop_scheduler(due=1)
        with patch(
            "code_indexer.global_repos.refresh_scheduler.check_query_admission",
            return_value=AdmissionDecision(allowed=False, retry_after_seconds=30),
        ) as gate:
            sched._scheduler_loop()
        assert gate.call_args.kwargs.get("record_denial") is False

    def test_the_query_path_still_counts_its_denials(self):
        """The default must stay True -- the handlers rely on it."""
        import inspect

        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        sig = inspect.signature(check_query_admission)
        assert sig.parameters["record_denial"].default is True

    def test_a_counted_denial_increments_and_an_uncounted_one_does_not(self):
        from code_indexer.server.services import query_admission_gate as gate_mod

        governor = MagicMock()
        governor.admission_allowed.return_value = False
        governor.last_red_min_dwell_seconds = 30.0
        cfg = MagicMock()
        cfg.job_admission_memory_gate_enabled = True
        cfg.job_admission_memory_max_used_pct = 80.0
        config_service = MagicMock()
        config_service.get_config.return_value.background_jobs_config = cfg

        with (
            patch(
                "code_indexer.server.services.memory_governor.get_memory_governor",
                return_value=governor,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=config_service,
            ),
        ):
            assert gate_mod.check_query_admission().allowed is False
            assert governor.increment_query_admissions_denied.call_count == 1

            assert gate_mod.check_query_admission(record_denial=False).allowed is False
            assert governor.increment_query_admissions_denied.call_count == 1
