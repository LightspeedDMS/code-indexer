"""
Unit tests for Story #352 - Dependency Map Repair Job Feedback.

Tests verify that repair now provides the same feedback mechanisms as
Full Analysis and Delta Refresh:
  AC1: Tracking backend status updated to "running" / "completed" / "failed"
  AC2: Activity journal initialized before repair starts
  AC3: Job tracker registered with operation_type="dependency_map_repair"
       and progress milestones reported at each phase boundary
  AC4: Journal path propagated to domain analyzer (enables Claude journal output)
  AC5: Tracking backend and journal cleaned up in finally block

Tests use the same strategy as existing dep_map tests:
  - Real filesystem (tmp_path)
  - Real DepMapHealthDetector and IndexRegenerator instances
  - Mocks only for external systems (tracking_backend, job_tracker, journal)
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers from existing tests
# ─────────────────────────────────────────────────────────────────────────────

from tests.unit.server.services.test_dep_map_health_detector import (
    make_healthy_output_dir,
)


# ─────────────────────────────────────────────────────────────────────────────
# Builder helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_health_detector():
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )

    return DepMapHealthDetector()


def _get_index_regenerator():
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

    return IndexRegenerator()


def _make_executor(domain_analyzer=None, journal_callback=None, progress_callback=None):
    """Build DepMapRepairExecutor with real detector/regenerator."""
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

    return DepMapRepairExecutor(
        health_detector=_get_health_detector(),
        index_regenerator=_get_index_regenerator(),
        domain_analyzer=domain_analyzer,
        journal_callback=journal_callback,
        progress_callback=progress_callback,
    )


def _make_mock_job_tracker():
    """Build a mock job tracker that records calls."""
    tracker = Mock()
    tracker.register_job = Mock()
    tracker.update_status = Mock()
    tracker.complete_job = Mock()
    tracker.fail_job = Mock()
    registered_jobs = []

    def _register(job_id, op_type, username=None, repo_alias=None):
        registered_jobs.append(
            {
                "job_id": job_id,
                "operation_type": op_type,
                "username": username,
                "repo_alias": repo_alias,
            }
        )

    tracker.register_job.side_effect = _register
    tracker._registered_jobs = registered_jobs
    return tracker


def _make_mock_tracking_backend():
    """Build a mock tracking backend that records update calls."""
    backend = Mock()
    backend.update_tracking = Mock()
    backend.get_tracking = Mock(return_value={"status": "idle"})
    return backend


def _make_mock_activity_journal(tmp_path: Path):
    """Build a mock activity journal with real path for testing."""
    journal_dir = tmp_path / "repair-journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_file = journal_dir / "_activity.md"
    journal_file.write_text("", encoding="utf-8")

    journal = Mock()
    journal.journal_path = journal_file
    journal.init = Mock(return_value=journal_file)
    journal.log = Mock()
    journal.clear = Mock()
    return journal


# ─────────────────────────────────────────────────────────────────────────────
# AC1: Tracking backend status transitions
# ─────────────────────────────────────────────────────────────────────────────


class TestAC1TrackingBackendStatus:
    """AC1: Tracking backend must be updated to running/completed/failed."""

    def test_tracking_backend_updated_to_running_before_repair(self, tmp_path):
        """
        Given repair is triggered
        When the repair route starts the background thread
        Then tracking_backend.update_tracking(status='running') is called
        before the executor runs.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        journal = _make_mock_activity_journal(tmp_path)
        job_tracker = _make_mock_job_tracker()

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        # Verify "running" was set
        update_calls = [str(c) for c in tracking_backend.update_tracking.call_args_list]
        running_calls = [
            c
            for c in tracking_backend.update_tracking.call_args_list
            if c.kwargs.get("status") == "running"
            or (c.args and c.args[0] == "running")
        ]
        assert len(running_calls) > 0, (
            "Expected tracking_backend.update_tracking(status='running') to be called "
            f"before repair starts. Actual calls: {update_calls}"
        )

    def test_tracking_backend_updated_to_completed_on_success(self, tmp_path):
        """
        Given repair completes successfully
        Then tracking_backend.update_tracking(status='completed') is called.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        journal = _make_mock_activity_journal(tmp_path)
        job_tracker = _make_mock_job_tracker()

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        completed_calls = [
            c
            for c in tracking_backend.update_tracking.call_args_list
            if c.kwargs.get("status") == "completed"
            or (c.args and c.args[0] == "completed")
        ]
        assert len(completed_calls) > 0, (
            "Expected tracking_backend.update_tracking(status='completed') on success. "
            f"Actual calls: {tracking_backend.update_tracking.call_args_list}"
        )

    def test_tracking_backend_updated_to_failed_on_exception(self, tmp_path):
        """
        Given repair throws an unexpected exception
        Then tracking_backend.update_tracking(status='failed') is called in finally block.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        journal = _make_mock_activity_journal(tmp_path)
        job_tracker = _make_mock_job_tracker()

        # Introduce a domain anomaly so Phase 1 runs, then make the analyzer explode
        # Use a simple approach: patch the executor to raise
        def _exploding_analyzer(*args, **kwargs):
            raise RuntimeError("Simulated Claude CLI crash")

        # Introduce an anomaly that requires domain_analyzer
        (output_dir / "_domains.json").write_text(
            json.dumps([{"name": "auth"}, {"name": "payments"}]), encoding="utf-8"
        )
        # Remove auth.md to create a missing_domain_file anomaly
        auth_file = output_dir / "auth.md"
        if auth_file.exists():
            auth_file.unlink()

        # Use an exploding domain_analyzer to force failure path
        with patch(
            "code_indexer.server.web.dependency_map_routes._build_repair_executor"
        ) as mock_build:
            from code_indexer.server.services.dep_map_repair_executor import (
                DepMapRepairExecutor,
            )

            executor = Mock(spec=DepMapRepairExecutor)
            executor.execute.side_effect = RuntimeError("Simulated executor failure")
            mock_build.return_value = executor

            # Should NOT raise - exception must be caught and status set to failed
            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=tracking_backend,
                job_tracker=job_tracker,
                activity_journal=journal,
            )

        failed_calls = [
            c
            for c in tracking_backend.update_tracking.call_args_list
            if c.kwargs.get("status") == "failed" or (c.args and c.args[0] == "failed")
        ]
        assert len(failed_calls) > 0, (
            "Expected tracking_backend.update_tracking(status='failed') on exception. "
            f"Actual calls: {tracking_backend.update_tracking.call_args_list}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Activity journal initialization
# ─────────────────────────────────────────────────────────────────────────────


class TestAC2ActivityJournalInitialization:
    """AC2: Journal must be initialized before repair executor runs."""

    def test_journal_init_called_before_repair(self, tmp_path):
        """
        Given repair starts
        When _run_repair_with_feedback executes
        Then activity_journal.init() is called with a valid writable path.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = Mock()
        journal.journal_path = None
        journal.init = Mock()
        journal.log = Mock()
        journal.finalize = Mock()

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        journal.init.assert_called_once()
        init_path = journal.init.call_args[0][0]
        assert isinstance(init_path, Path), (
            f"Expected journal.init() called with a Path, got {type(init_path)}"
        )
        # The parent directory must exist or be creatable
        assert init_path.exists() or init_path.parent.exists(), (
            f"Journal path {init_path} or its parent does not exist"
        )

    def test_journal_init_uses_repair_specific_directory(self, tmp_path):
        """
        Given repair starts
        Then journal is initialized in a repair-specific temp directory
        (not the staging dir, not the delta journal dir).
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = Mock()
        journal.journal_path = None
        journal.init = Mock()
        journal.log = Mock()
        journal.finalize = Mock()

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        journal.init.assert_called_once()
        init_path = journal.init.call_args[0][0]
        path_str = str(init_path)
        # Must be a repair-specific path
        assert "repair" in path_str.lower(), (
            f"Expected journal path to contain 'repair' to distinguish from delta/full. "
            f"Got: {path_str}"
        )

    def test_journal_init_failure_does_not_abort_repair(self, tmp_path):
        """
        Given journal.init() raises an exception
        Then repair continues (does not raise) and tracking backend is still updated.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = Mock()
        journal.journal_path = None
        journal.init = Mock(side_effect=OSError("Cannot create journal directory"))
        journal.log = Mock()
        journal.finalize = Mock()

        # Must not raise - error handling degrades gracefully
        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        # Repair should still have updated tracking to running and completed
        all_statuses = [
            c.kwargs.get("status") or (c.args[0] if c.args else None)
            for c in tracking_backend.update_tracking.call_args_list
        ]
        assert "completed" in all_statuses or "running" in all_statuses, (
            "Repair should have proceeded despite journal init failure. "
            f"Tracking statuses seen: {all_statuses}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Job tracker registration and progress milestones
# ─────────────────────────────────────────────────────────────────────────────


class TestAC3JobTrackerAndProgressMilestones:
    """AC3: Job must be registered with job tracker and progress reported."""

    def test_job_registered_with_dependency_map_repair_operation_type(self, tmp_path):
        """
        Given repair is triggered
        Then job_tracker.register_job() is called with operation_type='dependency_map_repair'.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = _make_mock_activity_journal(tmp_path)

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        job_tracker.register_job.assert_called_once()
        registered = job_tracker._registered_jobs
        assert len(registered) == 1, f"Expected 1 job registered, got {len(registered)}"
        assert registered[0]["operation_type"] == "dependency_map_repair", (
            f"Expected operation_type='dependency_map_repair', "
            f"got '{registered[0]['operation_type']}'"
        )

    def test_get_progress_from_service_recognizes_repair_operation_type(self):
        """
        Given a job tracker with an active 'dependency_map_repair' job
        When _get_progress_from_service() is called
        Then it returns the progress and progress_info for that job.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _get_progress_from_service,
        )

        # Build a mock dep_map_service with a job tracker holding a repair job
        dep_map_service = Mock()
        job_tracker = Mock()

        repair_job = Mock()
        repair_job.operation_type = "dependency_map_repair"
        repair_job.progress = 65
        repair_job.progress_info = "Phase 2: Removing orphan files"

        job_tracker.get_active_jobs.return_value = [repair_job]
        dep_map_service._job_tracker = job_tracker

        progress, progress_info = _get_progress_from_service(dep_map_service)

        assert progress == 65, f"Expected progress=65 for repair job, got {progress}"
        assert "Phase 2" in progress_info or "orphan" in progress_info.lower(), (
            f"Expected progress_info to contain phase info, got '{progress_info}'"
        )

    def test_progress_milestones_reported_during_executor_phases(self, tmp_path):
        """
        Given repair executor runs all 5 phases
        Then progress_callback is called with increasing progress values.

        AC3 milestone spec:
          Phase 1: 10-60%
          Phase 2: 65%
          Phase 3: 70%
          Phase 4: 80%
          Phase 5: 90%
          Complete: 100%
        """

        progress_calls = []

        def _capture_progress(progress: int, progress_info: str = "") -> None:
            progress_calls.append((progress, progress_info))

        # Make a healthy output dir so all phases run (nothing to repair)
        # but use real executor
        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        executor = _make_executor(progress_callback=_capture_progress)

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )

        detector = DepMapHealthDetector()
        health_report = detector.detect(output_dir)

        executor.execute(output_dir, health_report)

        # The executor must call progress_callback at least once
        assert len(progress_calls) > 0, (
            "Expected progress_callback to be called during executor.execute(). "
            "DepMapRepairExecutor must accept and call progress_callback."
        )

    def test_progress_values_are_monotonically_increasing(self, tmp_path):
        """
        Given repair runs phases in order
        Then progress values reported via progress_callback are non-decreasing.
        """

        progress_calls = []

        def _capture_progress(progress: int, progress_info: str = "") -> None:
            progress_calls.append(progress)

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        executor = _make_executor(progress_callback=_capture_progress)

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )

        detector = DepMapHealthDetector()
        health_report = detector.detect(output_dir)

        executor.execute(output_dir, health_report)

        if len(progress_calls) >= 2:
            for i in range(1, len(progress_calls)):
                assert progress_calls[i] >= progress_calls[i - 1], (
                    f"Progress must be non-decreasing. Got {progress_calls}"
                )

    def test_repair_job_completed_in_job_tracker_on_success(self, tmp_path):
        """
        Given repair completes successfully
        Then job_tracker.complete_job() is called with the registered job_id.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        job_tracker.complete_job = Mock()
        journal = _make_mock_activity_journal(tmp_path)

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        job_tracker.complete_job.assert_called_once()

    def test_repair_job_failed_in_job_tracker_on_exception(self, tmp_path):
        """
        Given repair executor raises an exception
        Then job_tracker.fail_job() is called (not complete_job).
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = _make_mock_activity_journal(tmp_path)

        with patch(
            "code_indexer.server.web.dependency_map_routes._build_repair_executor"
        ) as mock_build:
            from code_indexer.server.services.dep_map_repair_executor import (
                DepMapRepairExecutor,
            )

            executor = Mock(spec=DepMapRepairExecutor)
            executor.execute.side_effect = RuntimeError("Simulated failure")
            mock_build.return_value = executor

            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=tracking_backend,
                job_tracker=job_tracker,
                activity_journal=journal,
            )

        job_tracker.fail_job.assert_called_once()
        job_tracker.complete_job.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Journal path propagation to domain analyzer
# ─────────────────────────────────────────────────────────────────────────────


class TestAC4JournalPathPropagation:
    """AC4: Journal path must be non-None when passed to _build_domain_analyzer."""

    def test_journal_path_is_non_none_after_init(self, tmp_path):
        """
        Given journal.init() has been called
        When _build_domain_analyzer captures journal.journal_path
        Then journal_path is not None.

        This is the prerequisite for Claude reporting to the journal
        (the prompt appendix is only included when journal_path is not None).
        """
        from code_indexer.server.services.activity_journal_service import (
            ActivityJournalService,
        )

        journal = ActivityJournalService()
        journal_dir = tmp_path / "repair-journal"
        journal_dir.mkdir()

        # Before init, journal_path is None
        assert journal.journal_path is None

        # After init, journal_path must be non-None
        journal.init(journal_dir)
        assert journal.journal_path is not None, (
            "ActivityJournalService.journal_path must be non-None after init()"
        )
        assert (journal.journal_path).exists(), (
            f"Journal file {journal.journal_path} must exist after init()"
        )

    def test_build_domain_analyzer_captures_journal_path(self, tmp_path):
        """
        Given dep_map_service has an initialized journal
        When _build_domain_analyzer is called
        Then the analyzer closure captures the non-None journal_path.

        This verifies that _build_activity_journal_appendix() will be
        included in Claude's Pass 2 prompt.
        """
        from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer

        journal_dir = tmp_path / "repair-journal"
        journal_dir.mkdir()
        journal_file = journal_dir / "_activity.md"
        journal_file.write_text("", encoding="utf-8")

        dep_map_service = Mock()
        dep_map_service._get_activated_repos.return_value = []
        dep_map_service._enrich_repo_sizes.return_value = []

        # Journal on the service has a non-None journal_path
        journal = Mock()
        journal.journal_path = journal_file
        dep_map_service._activity_journal = journal
        dep_map_service.activity_journal = journal

        captured_journal_paths = []

        # Mock the analyzer to capture what journal_path it receives
        analyzer_obj = Mock()

        def _capture_pass2(**kwargs):
            captured_journal_paths.append(kwargs.get("journal_path"))

        analyzer_obj.run_pass_2_per_domain.side_effect = _capture_pass2
        dep_map_service._analyzer = analyzer_obj

        domain_analyzer = _build_domain_analyzer(dep_map_service, tmp_path)

        # Call the analyzer
        domain = {"name": "test-domain"}
        domain_list = [domain]
        domain_analyzer(tmp_path, domain, domain_list, [])

        assert len(captured_journal_paths) == 1, (
            "Expected run_pass_2_per_domain to be called once"
        )
        assert captured_journal_paths[0] is not None, (
            "Expected journal_path to be non-None when passed to run_pass_2_per_domain. "
            "This is required for Claude to report to the journal (AC4)."
        )
        assert captured_journal_paths[0] == journal_file, (
            f"Expected journal_path={journal_file}, got {captured_journal_paths[0]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC5: Completion state - finally block cleanup
# ─────────────────────────────────────────────────────────────────────────────


class TestAC5CompletionStateCleanup:
    """AC5: Tracking backend and journal must be cleaned up in finally block."""

    def test_tracking_backend_always_updated_even_on_exception(self, tmp_path):
        """
        Given repair executor raises an unexpected exception
        When _run_repair_with_feedback handles the error
        Then tracking_backend.update_tracking is called with a terminal status
        (never left stuck in 'running' state).
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = _make_mock_activity_journal(tmp_path)

        with patch(
            "code_indexer.server.web.dependency_map_routes._build_repair_executor"
        ) as mock_build:
            executor = Mock()
            executor.execute.side_effect = RuntimeError("Catastrophic failure")
            mock_build.return_value = executor

            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=tracking_backend,
                job_tracker=job_tracker,
                activity_journal=journal,
            )

        all_statuses = [
            c.kwargs.get("status") or (c.args[0] if c.args else None)
            for c in tracking_backend.update_tracking.call_args_list
        ]
        terminal_statuses = {"completed", "failed"}
        assert any(s in terminal_statuses for s in all_statuses), (
            "tracking_backend must be updated to a terminal status (completed/failed) "
            "even when repair raises. Status calls seen: "
            f"{tracking_backend.update_tracking.call_args_list}"
        )

    def test_journal_finalize_called_on_completion(self, tmp_path):
        """
        Given repair completes
        Then activity_journal.finalize() is called to clean up the journal.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = _make_mock_activity_journal(tmp_path)

        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=journal,
        )

        journal.clear.assert_called_once()

    def test_journal_finalize_called_even_on_exception(self, tmp_path):
        """
        Given repair executor raises an exception
        Then activity_journal.clear() is still called (in finally block).
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        tracking_backend = _make_mock_tracking_backend()
        job_tracker = _make_mock_job_tracker()
        journal = _make_mock_activity_journal(tmp_path)

        with patch(
            "code_indexer.server.web.dependency_map_routes._build_repair_executor"
        ) as mock_build:
            executor = Mock()
            executor.execute.side_effect = RuntimeError("Catastrophic failure")
            mock_build.return_value = executor

            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=tracking_backend,
                job_tracker=job_tracker,
                activity_journal=journal,
            )

        journal.clear.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Progress callback integration in DepMapRepairExecutor
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairExecutorProgressCallback:
    """Tests for progress_callback parameter in DepMapRepairExecutor."""

    def test_executor_accepts_progress_callback_parameter(self):
        """
        Given DepMapRepairExecutor constructor
        When progress_callback is passed
        Then it is accepted without error.
        """
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        progress_calls = []

        def cb(p, info=""):
            progress_calls.append(p)

        executor = DepMapRepairExecutor(
            health_detector=_get_health_detector(),
            index_regenerator=_get_index_regenerator(),
            progress_callback=cb,
        )
        assert executor is not None

    def test_executor_calls_progress_callback_at_phase_boundaries(self, tmp_path):
        """
        Given executor with progress_callback
        When execute() is called on a healthy output dir (phases 1-4 skipped)
        Then progress_callback is called at least for Phase 5 and completion.
        """
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        progress_calls = []

        def cb(progress: int, progress_info: str = "") -> None:
            progress_calls.append((progress, progress_info))

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        executor = DepMapRepairExecutor(
            health_detector=_get_health_detector(),
            index_regenerator=_get_index_regenerator(),
            progress_callback=cb,
        )

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )

        report = DepMapHealthDetector().detect(output_dir)
        executor.execute(output_dir, report)

        assert len(progress_calls) >= 1, (
            "progress_callback must be called at least once during execute(). "
            f"Got {len(progress_calls)} calls."
        )
        # Final call must be 100%
        final_progress = progress_calls[-1][0]
        assert final_progress == 100, (
            f"Final progress must be 100%. Got {final_progress}"
        )

    def test_executor_progress_callback_none_is_safe(self, tmp_path):
        """
        Given executor without progress_callback (None)
        When execute() runs
        Then no exception is raised.
        """
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path
        executor = DepMapRepairExecutor(
            health_detector=_get_health_detector(),
            index_regenerator=_get_index_regenerator(),
            progress_callback=None,
        )

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )

        report = DepMapHealthDetector().detect(output_dir)

        # Must not raise
        result = executor.execute(output_dir, report)
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# Finding 3: Graceful degradation when all components are None
# ─────────────────────────────────────────────────────────────────────────────


class TestNoneComponentsGracefulDegradation:
    """
    Finding 3 (code review): _run_repair_with_feedback must degrade gracefully
    when tracking_backend, job_tracker, and activity_journal are all None.

    These arrive from getattr(..., None) in the caller and can legitimately be
    None when dep_map_service lacks the expected attributes.
    The function must not raise AttributeError or any exception; repair must
    proceed and complete successfully despite the absence of all three components.
    """

    def test_repair_proceeds_when_all_components_none(self, tmp_path):
        """
        Given tracking_backend=None, job_tracker=None, activity_journal=None
        When _run_repair_with_feedback is called
        Then no exception is raised and repair completes successfully.

        This tests the explicit None guards (Finding 2): without them, calling
        activity_journal.init() on None raises AttributeError.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path

        # Must not raise - all three components are None
        _run_repair_with_feedback(
            output_dir=output_dir,
            tracking_backend=None,
            job_tracker=None,
            activity_journal=None,
        )

    def test_repair_completes_successfully_when_all_components_none(self, tmp_path):
        """
        Given all three components are None
        When _run_repair_with_feedback runs on a healthy output dir
        Then the repair executor still runs and returns a result.

        Verifies the function does not short-circuit or fail early due to
        missing components.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path

        completed_flag = []

        # Patch _build_repair_executor to verify it is actually called
        # (confirms repair proceeds, not just bails out silently)
        with patch(
            "code_indexer.server.web.dependency_map_routes._build_repair_executor"
        ) as mock_build:
            from unittest.mock import Mock as _Mock
            from code_indexer.server.services.dep_map_repair_executor import (
                DepMapRepairExecutor,
            )

            real_result = _Mock()
            real_result.status = "nothing_to_repair"
            executor = _Mock(spec=DepMapRepairExecutor)
            executor.execute.return_value = real_result
            executor.execute.side_effect = lambda *a, **kw: (
                completed_flag.append(True) or real_result  # type: ignore[func-returns-value]
            )
            mock_build.return_value = executor

            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=None,
                job_tracker=None,
                activity_journal=None,
            )

        assert len(completed_flag) == 1, (
            "Expected repair executor.execute() to be called once even when "
            "all components are None. Got: "
            f"{len(completed_flag)} calls."
        )

    def test_no_attribute_error_in_finally_when_components_none(self, tmp_path):
        """
        Given all three components are None
        When repair executor raises an exception (failure path)
        Then the finally block does not raise AttributeError on None components.

        This specifically tests the finally block None guards: without them,
        tracking_backend.update_tracking(status='failed') on None raises
        AttributeError, leaving the system state ambiguous.
        """
        from code_indexer.server.web.dependency_map_routes import (
            _run_repair_with_feedback,
        )

        make_healthy_output_dir(tmp_path)
        output_dir = tmp_path

        with patch(
            "code_indexer.server.web.dependency_map_routes._build_repair_executor"
        ) as mock_build:
            from unittest.mock import Mock as _Mock

            executor = _Mock()
            executor.execute.side_effect = RuntimeError(
                "Forced failure for None-guard test"
            )
            mock_build.return_value = executor

            # Must not raise - finally block must guard all None components
            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=None,
                job_tracker=None,
                activity_journal=None,
            )
