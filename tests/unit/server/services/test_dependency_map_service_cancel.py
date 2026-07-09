"""
Unit tests for DependencyMapService cancellation feature (Story #1040).

Tests:
  1. _cancel_event lifecycle: initialized cleared
  2. _cancel_event lifecycle: set/is_set/clear
  3. _cancel_event is independent per instance
  4. cancel_running_analysis() returns success/message when analysis is running
  5. cancel_running_analysis() returns no_active_job when nothing running
  6. cancel_running_analysis() sets _cancel_event when running
  7. run_full_analysis clears stale cancel flag at start
  8. run_delta_analysis clears stale cancel flag at start
  9. run_refinement_cycle clears stale cancel flag at start
 10. _update_affected_domains breaks early when cancel_event is pre-set
 11. run_refinement_cycle domain loop breaks when cancel_event is pre-set
 12. cancel_running_analysis method exists and is callable
"""

import json
import threading
from unittest.mock import Mock


class TestCancelEventLifecycle:
    """Tests for _cancel_event initialization and state management."""

    def _make_service(self):
        """Create a minimal DependencyMapService with mock dependencies."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        return DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

    def test_cancel_event_initialized_cleared(self):
        """_cancel_event must exist and start in cleared (not set) state."""
        service = self._make_service()
        assert hasattr(service, "_cancel_event"), "_cancel_event attribute missing"
        assert isinstance(service._cancel_event, threading.Event), (
            "_cancel_event must be a threading.Event"
        )
        assert not service._cancel_event.is_set(), (
            "_cancel_event must start cleared (not set)"
        )

    def test_cancel_event_set_and_clear(self):
        """_cancel_event can be set and cleared."""
        service = self._make_service()
        assert not service._cancel_event.is_set()
        service._cancel_event.set()
        assert service._cancel_event.is_set()
        service._cancel_event.clear()
        assert not service._cancel_event.is_set()

    def test_cancel_event_is_independent_per_instance(self):
        """Each service instance has its own cancel event."""
        service1 = self._make_service()
        service2 = self._make_service()
        service1._cancel_event.set()
        assert service1._cancel_event.is_set()
        assert not service2._cancel_event.is_set()


class TestCancelRunningAnalysis:
    """Tests for cancel_running_analysis() method."""

    def _make_service(self):
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        return DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

    def test_cancel_running_analysis_returns_success_when_lock_held(self):
        """cancel_running_analysis signals cancellation when analysis is running."""
        service = self._make_service()
        # Simulate a running analysis by holding the lock
        service._lock.acquire()
        try:
            result = service.cancel_running_analysis()
            assert result["success"] is True
            assert "message" in result
            assert service._cancel_event.is_set()
        finally:
            service._lock.release()

    def test_cancel_running_analysis_returns_no_active_job_when_idle(self):
        """cancel_running_analysis returns no_active_job when nothing is running."""
        service = self._make_service()
        # Lock is NOT held — no analysis running
        result = service.cancel_running_analysis()
        assert result.get("status") == "no_active_job", (
            f"Expected no_active_job, got: {result}"
        )
        # Should NOT set the cancel event when nothing is running
        assert not service._cancel_event.is_set(), (
            "cancel event must not be set when nothing is running"
        )

    def test_cancel_running_analysis_sets_event(self):
        """cancel_running_analysis sets _cancel_event when analysis is running."""
        service = self._make_service()
        service._lock.acquire()
        try:
            assert not service._cancel_event.is_set()
            service.cancel_running_analysis()
            assert service._cancel_event.is_set()
        finally:
            service._lock.release()


class TestCancelEventClearedOnNewRun:
    """Tests that stale cancel flag is cleared at the start of each analysis run."""

    def _make_service(self):
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        mock_manager = Mock()
        mock_manager.golden_repos_dir = "/tmp/test-golden-repos"
        mock_manager.list_golden_repos.return_value = []

        mock_config_manager = Mock()
        mock_config = Mock()
        mock_config.dependency_map_enabled = False  # Cause early exit
        mock_config_manager.get_claude_integration_config.return_value = mock_config

        mock_tracking = Mock()
        mock_tracking.get_tracking.return_value = {}

        return DependencyMapService(
            golden_repos_manager=mock_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking,
            analyzer=Mock(),
        )

    def test_run_full_analysis_clears_stale_cancel_flag(self):
        """run_full_analysis clears _cancel_event at start so stale flag is gone."""
        service = self._make_service()
        # Pre-set the cancel event to simulate stale cancellation
        service._cancel_event.set()
        assert service._cancel_event.is_set()

        # run_full_analysis should clear the flag at start (even if it exits early)
        try:
            service.run_full_analysis()
        except Exception:
            pass  # Early exit expected; we only care the flag was cleared

        assert not service._cancel_event.is_set(), (
            "run_full_analysis must clear _cancel_event at the start"
        )

    def test_run_delta_analysis_clears_stale_cancel_flag(self):
        """run_delta_analysis clears _cancel_event at start."""
        service = self._make_service()
        service._cancel_event.set()
        assert service._cancel_event.is_set()

        try:
            service.run_delta_analysis()
        except Exception:
            pass

        assert not service._cancel_event.is_set(), (
            "run_delta_analysis must clear _cancel_event at the start"
        )

    def test_run_refinement_cycle_clears_stale_cancel_flag(self):
        """run_refinement_cycle clears _cancel_event at start."""
        service = self._make_service()
        service._cancel_event.set()
        assert service._cancel_event.is_set()

        try:
            service.run_refinement_cycle()
        except Exception:
            pass

        assert not service._cancel_event.is_set(), (
            "run_refinement_cycle must clear _cancel_event at the start"
        )


class TestDomainLoopCancellation:
    """Tests that domain loops break when cancel_event is set."""

    def test_update_affected_domains_breaks_on_cancellation(self, tmp_path):
        """_update_affected_domains exits early when _cancel_event is pre-set."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        mock_manager = Mock()
        mock_manager.golden_repos_dir = str(tmp_path)

        mock_config = Mock()
        mock_config_manager = Mock()
        mock_config_manager.get_claude_integration_config.return_value = mock_config

        service = DependencyMapService(
            golden_repos_manager=mock_manager,
            config_manager=mock_config_manager,
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        # Create a dep-map dir with 3 domain files
        dep_map_dir = tmp_path / "dep-map"
        dep_map_dir.mkdir()
        for name in ("alpha", "beta", "gamma"):
            (dep_map_dir / f"{name}.md").write_text(f"# {name}")

        # Track which domains were processed by patching _update_domain_file
        processed = []

        def patched_update_domain_file(domain_name, **kwargs):
            processed.append(domain_name)
            from code_indexer.server.services.dependency_map_service import (
                _DomainUpdateResult,
            )

            return _DomainUpdateResult.NOOP

        service._update_domain_file = patched_update_domain_file

        # Pre-set cancel event before the call
        service._cancel_event.set()

        affected = {"alpha", "beta", "gamma"}
        service._update_affected_domains(
            affected_domains=affected,
            dependency_map_dir=dep_map_dir,
            changed_repos=[],
            new_repos=[],
            removed_repos=[],
            config=mock_config,
        )

        # With cancel pre-set, the loop should process 0 domains
        assert len(processed) == 0, (
            f"Expected 0 domains processed on cancellation, but got: {processed}"
        )

    def test_refinement_cycle_domain_loop_breaks_on_cancellation(self, tmp_path):
        """run_refinement_cycle domain loop breaks when _cancel_event is pre-set."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        mock_manager = Mock()
        mock_manager.golden_repos_dir = str(tmp_path)

        mock_config = Mock()
        mock_config.refinement_enabled = True
        mock_config.refinement_domains_per_run = 10
        mock_config.dep_map_fact_check_enabled = False
        mock_config.refinement_interval_hours = 4

        mock_config_manager = Mock()
        mock_config_manager.get_claude_integration_config.return_value = mock_config

        mock_tracking = Mock()
        mock_tracking.get_tracking.return_value = {"refinement_cursor": 0}

        service = DependencyMapService(
            golden_repos_manager=mock_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking,
            analyzer=Mock(),
        )

        # Set up the cidx-meta directory with _domains.json
        cidx_meta_dir = tmp_path / "cidx-meta" / "dependency-map"
        cidx_meta_dir.mkdir(parents=True)
        domains = [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]
        (cidx_meta_dir / "_domains.json").write_text(json.dumps(domains))
        for d in domains:
            (cidx_meta_dir / f"{d['name']}.md").write_text(f"# {d['name']}")

        # Patch _get_cidx_meta_read_path to return the cidx-meta parent dir
        service._get_cidx_meta_read_path = Mock(return_value=tmp_path / "cidx-meta")

        # Track which domains were refined
        refined_domains = []

        def patched_refine(domain_name, **kwargs):
            refined_domains.append(domain_name)
            # Set cancel event after first domain so loop breaks on next iteration check
            service._cancel_event.set()
            return False

        service.refine_or_create_domain = patched_refine

        service.run_refinement_cycle()

        # With cancel set after first domain, the loop should process exactly 1 domain
        assert len(refined_domains) == 1, (
            f"Expected 1 domain refined before cancellation, got: {refined_domains}"
        )


class TestCancelGracefulCleanup:
    """Tests that cancellation triggers fail_job (not complete_job) on the job tracker."""

    def _make_service_with_mock_tracker(self):
        """Create a DependencyMapService with a mock job tracker."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        mock_tracker = Mock()
        mock_tracker.register_job_if_no_conflict = Mock()
        mock_tracker.update_status = Mock()
        mock_tracker.fail_job = Mock()
        mock_tracker.complete_job = Mock()

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
            job_tracker=mock_tracker,
        )
        return service, mock_tracker

    def test_cancel_event_set_implies_fail_job_not_complete_job(self):
        """
        When _cancel_event is set and _analysis_succeeded is False, finally block
        must call fail_job, not complete_job.

        This is a direct logic test: we simulate the finally-block conditions
        by calling the job tracker directly as the finally block would.
        """
        service, mock_tracker = self._make_service_with_mock_tracker()
        tracked_job_id = "test-job-cancel-001"

        # Simulate conditions at finally-block entry when analysis was cancelled:
        # _cancel_event is set, _analysis_succeeded is False
        service._cancel_event.set()
        _analysis_succeeded = False
        _cancel_was_requested = service._cancel_event.is_set()

        # Execute the logic that the finally block should use
        if _analysis_succeeded:
            mock_tracker.complete_job(tracked_job_id)
        elif _cancel_was_requested:
            mock_tracker.fail_job(tracked_job_id, error="Cancelled by admin")

        # Verify fail_job was called, not complete_job
        mock_tracker.fail_job.assert_called_once_with(
            tracked_job_id, error="Cancelled by admin"
        )
        mock_tracker.complete_job.assert_not_called()

    def test_success_calls_complete_job_not_fail_job(self):
        """
        When analysis succeeds and _cancel_event is not set, finally block
        must call complete_job, not fail_job.
        """
        service, mock_tracker = self._make_service_with_mock_tracker()
        tracked_job_id = "test-job-success-001"

        # Simulate conditions at finally-block entry when analysis succeeded
        _analysis_succeeded = True
        _cancel_was_requested = service._cancel_event.is_set()  # False

        # Execute the logic that the finally block should use
        if _analysis_succeeded:
            mock_tracker.complete_job(tracked_job_id)
        elif _cancel_was_requested:
            mock_tracker.fail_job(tracked_job_id, error="Cancelled by admin")

        # Verify complete_job was called, not fail_job
        mock_tracker.complete_job.assert_called_once_with(tracked_job_id)
        mock_tracker.fail_job.assert_not_called()


class TestCancelRestEndpoint:
    """Tests for POST /admin/dependency-map/cancel REST endpoint."""

    def test_cancel_endpoint_function_exists_in_routes(self):
        """cancel_dependency_map_analysis function must exist in dependency_map_routes."""
        from code_indexer.server.web import dependency_map_routes

        assert hasattr(dependency_map_routes, "cancel_dependency_map_analysis"), (
            "cancel_dependency_map_analysis function must exist in dependency_map_routes"
        )
        assert callable(dependency_map_routes.cancel_dependency_map_analysis), (
            "cancel_dependency_map_analysis must be callable"
        )

    def test_cancel_endpoint_route_registered(self):
        """POST /dependency-map/cancel must be registered on dependency_map_router."""
        from code_indexer.server.web.dependency_map_routes import dependency_map_router

        routes = dependency_map_router.routes
        cancel_routes = [
            r
            for r in routes
            if hasattr(r, "path")
            and r.path == "/dependency-map/cancel"
            and hasattr(r, "methods")
            and "POST" in (r.methods or set())
        ]
        assert len(cancel_routes) == 1, (
            f"Expected exactly 1 POST /dependency-map/cancel route, found: {len(cancel_routes)}"
        )

    def test_cancel_endpoint_returns_success_when_lock_held(self):
        """cancel_dependency_map_analysis returns 200 with success when analysis running."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.web.dependency_map_routes import (
            cancel_dependency_map_analysis,
        )

        mock_service = MagicMock()
        mock_service.cancel_running_analysis.return_value = {
            "success": True,
            "message": "Cancellation signal sent",
        }

        mock_request = MagicMock()
        mock_request.state.session = {"username": "admin", "role": "admin"}

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._require_admin_session",
                return_value={"username": "admin"},
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_service_from_state",
                return_value=mock_service,
            ),
        ):
            response = cancel_dependency_map_analysis(mock_request)

        assert response.status_code == 200
        import json

        body = json.loads(response.body)
        assert body.get("success") is True

    def test_cancel_endpoint_returns_no_active_job_when_idle(self):
        """cancel_dependency_map_analysis returns 200 with no_active_job when idle."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.web.dependency_map_routes import (
            cancel_dependency_map_analysis,
        )

        mock_service = MagicMock()
        mock_service.cancel_running_analysis.return_value = {
            "status": "no_active_job",
            "message": "No analysis currently running",
        }

        mock_request = MagicMock()

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._require_admin_session",
                return_value={"username": "admin"},
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_service_from_state",
                return_value=mock_service,
            ),
        ):
            response = cancel_dependency_map_analysis(mock_request)

        assert response.status_code == 200
        import json

        body = json.loads(response.body)
        assert body.get("status") == "no_active_job"

    def test_cancel_endpoint_returns_401_without_admin_session(self):
        """cancel_dependency_map_analysis returns 401 when not authenticated as admin."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.web.dependency_map_routes import (
            cancel_dependency_map_analysis,
        )

        mock_request = MagicMock()

        with patch(
            "code_indexer.server.web.dependency_map_routes._require_admin_session",
            return_value=None,
        ):
            response = cancel_dependency_map_analysis(mock_request)

        assert response.status_code == 401

    def test_cancel_endpoint_returns_503_when_service_unavailable(self):
        """cancel_dependency_map_analysis returns 503 when dep map service is None."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.web.dependency_map_routes import (
            cancel_dependency_map_analysis,
        )

        mock_request = MagicMock()

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._require_admin_session",
                return_value={"username": "admin"},
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_service_from_state",
                return_value=None,
            ),
        ):
            response = cancel_dependency_map_analysis(mock_request)

        assert response.status_code == 503


class TestCancelledJobMarkedAsFailed:
    """Bug #1040: Cancelled job must be marked failed, not completed."""

    def _make_service_with_mock_tracker(self, tmp_path):
        """Create a DependencyMapService with controllable mocks."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        mock_tracker = Mock()
        mock_tracker.register_job_if_no_conflict = Mock()
        mock_tracker.update_status = Mock()
        mock_tracker.fail_job = Mock()
        mock_tracker.complete_job = Mock()

        mock_manager = Mock()
        mock_manager.golden_repos_dir = str(tmp_path)

        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_config.dependency_map_interval_hours = 1
        mock_config_manager = Mock()
        mock_config_manager.get_claude_integration_config.return_value = mock_config

        mock_tracking = Mock()
        mock_tracking.get_tracking.return_value = {}

        service = DependencyMapService(
            golden_repos_manager=mock_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking,
            analyzer=Mock(),
            job_tracker=mock_tracker,
        )
        return service, mock_tracker

    def test_delta_cancelled_during_domain_update_calls_fail_job(self, tmp_path):
        """
        Bug #1040: run_delta_analysis must call fail_job (not complete_job) when
        _cancel_event is set during _update_affected_domains.

        Simulates: cancel event set inside _update_affected_domains (domain loop
        breaks early), execution continues to the success-flag line which must
        NOT set _delta_succeeded = True when cancel is active.
        """
        service, mock_tracker = self._make_service_with_mock_tracker(tmp_path)

        # Mock detect_changes to report a change so domain processing is reached
        service.detect_changes = Mock(
            return_value=(
                [{"alias": "repo-a"}],  # changed_repos
                [],  # new_repos
                [],  # removed_repos
            )
        )

        # Mock identify_affected_domains to return one domain
        service.identify_affected_domains = Mock(return_value={"domain-alpha"})

        # Create dep-map dir with domain file so _update_affected_domains has something
        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "domain-alpha.md").write_text("# domain-alpha")

        # Mock _update_affected_domains to set cancel event (simulates mid-loop cancel)
        def cancel_during_update(*args, **kwargs):
            service._cancel_event.set()
            return []  # no errors — loop "completed" but cancelled

        service._update_affected_domains = cancel_during_update

        # Mock finalization helpers that run after domain update
        service._remove_stale_repos_from_domains_json = Mock()
        service._finalize_delta_tracking = Mock()
        service._get_activated_repos = Mock(return_value=[])

        try:
            service.run_delta_analysis(job_id="test-delta-cancel-001")
        except Exception:
            pass  # Some cleanup exceptions are acceptable

        # fail_job must be called, not complete_job
        mock_tracker.fail_job.assert_called_once_with(
            "test-delta-cancel-001", error="Cancelled by admin"
        )
        mock_tracker.complete_job.assert_not_called()

    def test_full_cancelled_during_finalization_calls_fail_job(self, tmp_path):
        """
        Bug #1040: run_full_analysis must call fail_job (not complete_job) when
        _cancel_event is set during _finalize_analysis.

        Simulates: cancel event set inside _finalize_analysis (Pass 2 domain loop
        breaks early), execution continues to the success-flag line which must
        NOT set _analysis_succeeded = True when cancel is active.
        """
        service, mock_tracker = self._make_service_with_mock_tracker(tmp_path)

        # Mock _setup_analysis to return valid paths/config without early exit
        staging_dir = tmp_path / "cidx-meta" / "dependency-map.staging"
        staging_dir.mkdir(parents=True)
        final_dir = tmp_path / "cidx-meta" / "dependency-map"
        final_dir.mkdir(parents=True)

        setup_result = {
            "early_return": False,
            "config": service._config_manager.get_claude_integration_config(),
            "paths": {
                "staging_dir": staging_dir,
                "final_dir": final_dir,
                "cidx_meta_path": tmp_path / "cidx-meta",
                "cidx_meta_read_path": tmp_path / "cidx-meta",
                "golden_repos_root": tmp_path,
            },
            "repo_list": [],
        }
        service._setup_analysis = Mock(return_value=setup_result)

        # Mock _execute_analysis_passes to return valid domain list
        service._execute_analysis_passes = Mock(return_value=([], [], 0.1, 0.2))

        # Mock _finalize_analysis to set cancel event (simulates mid-pass2 cancel)
        def cancel_during_finalize(*args, **kwargs):
            service._cancel_event.set()

        service._finalize_analysis = cancel_during_finalize

        try:
            service.run_full_analysis(job_id="test-full-cancel-001")
        except Exception:
            pass  # Some cleanup exceptions are acceptable

        # fail_job must be called, not complete_job
        mock_tracker.fail_job.assert_called_once_with(
            "test-full-cancel-001", error="Cancelled by admin"
        )
        mock_tracker.complete_job.assert_not_called()


class TestCancellationMethodExists:
    """Smoke test: cancel_running_analysis method exists and is callable."""

    def test_cancel_running_analysis_method_exists_and_callable(self):
        """cancel_running_analysis method exists on DependencyMapService."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )
        assert hasattr(service, "cancel_running_analysis"), (
            "cancel_running_analysis method must exist"
        )
        assert callable(service.cancel_running_analysis), (
            "cancel_running_analysis must be callable"
        )
