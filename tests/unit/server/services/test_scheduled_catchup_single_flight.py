"""The scheduled catch-up sweep must run once per cluster, not once per process.

`register_job` has no conflict check, and the `except Exception` around it
only nulls `tracked_job_id` -- the work then runs regardless. So registration
was dashboard decoration, not a claim, and the sweep executed on every process.

That is 12 concurrent runs in the tools-* deployment: 6 replicas x uvicorn
`--workers 2`, since the lifespan runs once per worker process. Every other
fleet-wide sweep in this codebase guards itself, most via the
`idx_active_job_per_repo` partial unique index on (operation_type, repo_alias);
this one opted out.

Default-off today (`config_manager.py`), so latent rather than live -- which is
the only reason it has not been noticed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import DuplicateJobError


def _service(tracker):
    from code_indexer.server.services.scheduled_catchup_service import (
        ScheduledCatchupService,
    )

    svc = ScheduledCatchupService.__new__(ScheduledCatchupService)
    svc._job_tracker = tracker
    return svc


class TestCatchupClaimsBeforeWorking:
    def test_a_duplicate_claim_skips_the_work(self):
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.side_effect = DuplicateJobError(
            "scheduled_catchup", "server", "other-pod-job"
        )
        svc = _service(tracker)
        manager = MagicMock()
        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=manager,
        ):
            svc._process_catchup()
        manager.process_all_fallbacks.assert_not_called()

    def test_an_uncontended_claim_runs_the_work(self):
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.return_value = MagicMock(job_id="j1")
        svc = _service(tracker)
        manager = MagicMock()
        manager.process_all_fallbacks.return_value = MagicMock(processed=[], error=None)
        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=manager,
        ):
            svc._process_catchup()
        manager.process_all_fallbacks.assert_called_once()

    def test_it_claims_with_the_conflict_aware_api(self):
        """`register_job` cannot single-flight: pinned so a future edit cannot
        quietly swap back to it."""
        import inspect

        from code_indexer.server.services.scheduled_catchup_service import (
            ScheduledCatchupService,
        )

        src = inspect.getsource(ScheduledCatchupService)
        assert "register_job_if_no_conflict" in src
        assert "self._job_tracker.register_job(" not in src

    def test_a_broken_tracker_does_not_stop_the_sweep(self):
        """Fail OPEN on anything that is not a duplicate. A tracker outage must
        not mean catch-up never runs anywhere -- that trades a load problem for
        a correctness one."""
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.side_effect = RuntimeError("tracker down")
        svc = _service(tracker)
        manager = MagicMock()
        manager.process_all_fallbacks.return_value = MagicMock(processed=[], error=None)
        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=manager,
        ):
            svc._process_catchup()
        manager.process_all_fallbacks.assert_called_once()
