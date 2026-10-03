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

import pytest

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


class TestTheLoopActuallyConsultsIt:
    """Pins the CALL SITE, not just the helper.

    A helper that returns the right answer while nothing calls it is the
    failure mode this session has already hit twice (a guard not wired into
    CI, and a Dockerfile assertion that compared a value to itself). The
    helper tests above would all pass with the scheduler loop untouched.
    """

    def test_the_scheduler_loop_defers_on_a_denied_admission(self) -> None:
        import inspect

        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        src = inspect.getsource(RefreshScheduler._scheduler_loop)
        assert "_refresh_admission_allowed()" in src, (
            "the refresh loop must consult the admission gate before submitting"
        )
        assert "due_repos = []" in src, (
            "a denied admission must actually drop the batch, not just log"
        )

    def test_next_refresh_is_not_advanced_when_deferring(self) -> None:
        """Deferring must be free. If the loop advanced next_refresh while
        skipping the work, a pod under sustained pressure would silently drop
        whole refresh cycles instead of retrying them."""
        import inspect

        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        src = inspect.getsource(RefreshScheduler._scheduler_loop)
        gate = src.index("_refresh_admission_allowed()")
        loop = src.index("for repo in due_repos:")
        assert gate < loop, "the gate must run before the submit loop"
        between = src[gate:loop]
        assert "next_refresh" not in between or "unadvanced" in between, (
            "deferring must not advance next_refresh"
        )
