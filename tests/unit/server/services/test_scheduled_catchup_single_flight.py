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

from code_indexer.server.services.job_tracker import DuplicateJobError


def _service(tracker):
    from code_indexer.server.services.scheduled_catchup_service import (
        ScheduledCatchupService,
    )

    svc = ScheduledCatchupService.__new__(ScheduledCatchupService)
    svc._job_tracker = tracker
    return svc


def _run(svc, manager):
    """Drive one _process_catchup() with `manager` as the CLI manager."""
    with patch(
        "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
        return_value=manager,
    ):
        svc._process_catchup()


def _ok_manager():
    manager = MagicMock()
    manager.process_all_fallbacks.return_value = MagicMock(processed=[], error=None)
    return manager


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
        """`register_job` cannot single-flight -- it has no conflict check.

        Asserted behaviourally (which call the tracker actually receives)
        rather than by reading the source: a source-text check also passes when
        the call is only mentioned in a comment, and fails on a harmless
        rename.
        """
        tracker = MagicMock()
        svc = _service(tracker)
        _run(svc, _ok_manager())
        tracker.register_job_if_no_conflict.assert_called_once()
        tracker.register_job.assert_not_called()

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


class TestTheClaimIsAlwaysReleased:
    """A claim that is never released is a permanent cluster-wide stop.

    ``idx_active_job_per_repo`` is ``WHERE status IN ('pending','running')``, so
    an abandoned row keeps matching forever. Every later ``_process_catchup()``
    on every process then raises ``DuplicateJobError`` against that orphan and
    returns -- catch-up never runs again anywhere until the row is deleted by
    hand. Node-scoped startup orphan cleanup only helps after a restart.

    So a single-flight claim is only half the mechanism; these pin the other half.
    """

    def test_a_successful_sweep_completes_the_claim(self):
        tracker = MagicMock()
        svc = _service(tracker)
        _run(svc, _ok_manager())
        (job_id,) = tracker.register_job_if_no_conflict.call_args.args[:1]
        tracker.complete_job.assert_called_once_with(job_id)

    def test_a_failing_sweep_fails_the_claim(self):
        tracker = MagicMock()
        svc = _service(tracker)
        manager = MagicMock()
        manager.process_all_fallbacks.side_effect = RuntimeError("sweep exploded")
        _run(svc, manager)
        (job_id,) = tracker.register_job_if_no_conflict.call_args.args[:1]
        tracker.fail_job.assert_called_once()
        assert tracker.fail_job.call_args.args[0] == job_id

    def test_update_status_failing_after_a_successful_claim_still_releases_it(self):
        """The regression this split exists to prevent.

        Registration and ``update_status`` used to share one ``try``. A claim
        that succeeded followed by an ``update_status`` that raised landed in
        the fail-open branch, which nulled ``tracked_job_id`` -- and because
        both ``complete_job`` and ``fail_job`` are guarded by
        ``if tracked_job_id``, the row was orphaned in 'pending' with nothing
        left to release it.
        """
        tracker = MagicMock()
        tracker.update_status.side_effect = RuntimeError("transient db error")
        svc = _service(tracker)
        manager = _ok_manager()
        _run(svc, manager)

        manager.process_all_fallbacks.assert_called_once()
        (job_id,) = tracker.register_job_if_no_conflict.call_args.args[:1]
        tracker.complete_job.assert_called_once_with(job_id)

    def test_a_failed_claim_attempt_releases_nothing(self):
        """Nothing was created, so completing or failing a row would be a lie.

        The id the attempt would have used never reached the tracker; calling
        complete_job on it could collide with a real row on another node.
        """
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.side_effect = RuntimeError("tracker down")
        svc = _service(tracker)
        _run(svc, _ok_manager())
        tracker.complete_job.assert_not_called()
        tracker.fail_job.assert_not_called()

    def test_a_duplicate_releases_nothing(self):
        """The row belongs to another process -- completing it would steal it."""
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.side_effect = DuplicateJobError(
            "scheduled_catchup", "server", "other-pod-job"
        )
        svc = _service(tracker)
        _run(svc, _ok_manager())
        tracker.complete_job.assert_not_called()
        tracker.fail_job.assert_not_called()
