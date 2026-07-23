"""Unit tests for MaintenanceState service - Core functionality.

Story #734: Job-Aware Auto-Update with Graceful Drain Mode
Tests AC1 (Server Maintenance Mode API) - Basic operations.
"""

import pytest
from unittest.mock import MagicMock


# Constants for thread safety tests
THREAD_COUNT = 5
ITERATIONS_PER_THREAD = 100


def _maintenance_state_reset_cycle():
    """Reset-before/reset-after generator for the MaintenanceState singleton.

    Bug #1446: factored out of the autouse fixture below (rather than living
    only as fixture body) so TestMaintenanceStateAutouseTeardownBug1446 can
    drive it directly as a plain generator, bypassing pytest's "fixtures are
    not meant to be called directly" guard on @pytest.fixture-decorated
    objects while still exercising the exact same reset logic used in real
    test runs.
    """
    from code_indexer.server.services.maintenance_service import (
        _reset_maintenance_state,
    )

    _reset_maintenance_state()
    yield
    _reset_maintenance_state()


@pytest.fixture(autouse=True)
def _reset_maintenance_state_before_and_after():
    """Bug #1446: reset the MaintenanceState singleton before AND after every
    test in this file.

    Every test below already calls _reset_maintenance_state() manually at its
    own start (left in place — redundant with the reset-BEFORE step here, but
    harmless, and it keeps each test readable in isolation). What was MISSING
    was a reset-AFTER step: without it, a test in this file that enters
    maintenance mode and never explicitly exits/resets leaves the
    module-level singleton in is_maintenance_mode() == True. If pytest then
    schedules an unrelated test (e.g. in test_pod_pull_executing_node_bug1430.py
    or test_pod_pull_work_stealing_roundtrip_bug1424.py) immediately
    afterward within the same worker process, that unrelated test's
    BackgroundJobManager.submit_job() call raises MaintenanceModeError even
    though it has nothing to do with maintenance mode.
    """
    yield from _maintenance_state_reset_cycle()


class TestMaintenanceStateBasics:
    """Test basic MaintenanceState functionality."""

    def test_singleton_pattern(self):
        """MaintenanceState should be a singleton."""
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state1 = get_maintenance_state()
        state2 = get_maintenance_state()
        assert state1 is state2

    def test_initial_state_is_not_maintenance(self):
        """MaintenanceState should not be in maintenance mode initially."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()
        assert state.is_maintenance_mode() is False

    def test_enter_maintenance_mode(self):
        """Should be able to enter maintenance mode."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()

        result = state.enter_maintenance_mode()

        assert state.is_maintenance_mode() is True
        assert result["maintenance_mode"] is True
        assert "entered_at" in result

    def test_restart_clears_maintenance_mode(self):
        """AC5: Server restart (simulated via _reset) should clear maintenance mode."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        # Enter maintenance mode
        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()
        assert state.is_maintenance_mode() is True

        # Simulate server restart
        _reset_maintenance_state()

        # New state should NOT be in maintenance mode
        new_state = get_maintenance_state()
        assert new_state.is_maintenance_mode() is False
        assert new_state.get_status()["entered_at"] is None


class TestMaintenanceStateExitAndStatus:
    """Test exit and status operations."""

    def test_exit_maintenance_mode(self):
        """Should be able to exit maintenance mode."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        result = state.exit_maintenance_mode()

        assert state.is_maintenance_mode() is False
        assert result["maintenance_mode"] is False
        assert "message" in result

    def test_get_status_when_not_in_maintenance(self):
        """get_status should return correct info when not in maintenance."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()

        status = state.get_status()

        assert status["maintenance_mode"] is False
        assert status["entered_at"] is None
        assert "drained" in status

    def test_get_status_when_in_maintenance(self):
        """get_status should return correct info when in maintenance."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        status = state.get_status()

        assert status["maintenance_mode"] is True
        assert status["entered_at"] is not None
        assert "running_jobs" in status
        assert "queued_jobs" in status


class TestMaintenanceStateDrain:
    """Test drain status functionality (AC2)."""

    def test_is_drained_when_no_jobs(self):
        """System should be drained when no running or queued jobs."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        assert state.is_drained() is True

    def test_get_drain_status_response(self):
        """get_drain_status should return proper format."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()

        drain_status = state.get_drain_status()

        assert "drained" in drain_status
        assert "running_jobs" in drain_status
        assert "queued_jobs" in drain_status
        assert "estimated_drain_seconds" in drain_status

    def test_is_drained_with_running_jobs(self):
        """System should NOT be drained when running jobs exist."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )

        _reset_maintenance_state()
        state = get_maintenance_state()

        mock_tracker = MagicMock()
        mock_tracker.get_running_jobs_count.return_value = 1
        mock_tracker.get_queued_jobs_count.return_value = 0

        state.register_job_tracker(mock_tracker)
        state.enter_maintenance_mode()

        assert state.is_drained() is False


class TestSyncJobManagerMaintenanceIntegration:
    """Test SyncJobManager maintenance mode integration."""

    def test_sync_job_manager_rejects_during_maintenance(self):
        """SyncJobManager should raise error when in maintenance mode."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )
        from code_indexer.server.jobs.manager import SyncJobManager
        from code_indexer.server.jobs.models import JobType
        from code_indexer.server.jobs.exceptions import MaintenanceModeError

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        manager = SyncJobManager()

        with pytest.raises(MaintenanceModeError) as exc_info:
            manager.create_job(
                username="testuser",
                user_alias="Test User",
                job_type=JobType.REPOSITORY_SYNC,
            )

        assert "maintenance" in str(exc_info.value).lower()

    def test_background_job_manager_rejects_during_maintenance(self):
        """BackgroundJobManager should raise error when in maintenance mode."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )
        from code_indexer.server.jobs.exceptions import MaintenanceModeError

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        manager = BackgroundJobManager()

        def dummy_func():
            return {"status": "done"}

        with pytest.raises(MaintenanceModeError) as exc_info:
            manager.submit_job(
                operation_type="test_operation",
                func=dummy_func,
                submitter_username="testuser",
            )

        assert "maintenance" in str(exc_info.value).lower()

    def test_golden_repo_manager_add_rejects_during_maintenance(self):
        """GoldenRepoManager.add_golden_repo should raise error during maintenance."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )
        from code_indexer.server.jobs.exceptions import MaintenanceModeError
        import tempfile

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GoldenRepoManager(data_dir=tmpdir)

            with pytest.raises(MaintenanceModeError) as exc_info:
                manager.add_golden_repo(
                    repo_url="https://github.com/test/repo.git",
                    alias="test-repo",
                    submitter_username="testuser",
                )

            assert "maintenance" in str(exc_info.value).lower()

    def test_refresh_scheduler_job_submission_rejected_during_maintenance(self):
        """RefreshScheduler job submission via BackgroundJobManager is rejected during maintenance."""
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )
        from code_indexer.server.jobs.exceptions import MaintenanceModeError
        from unittest.mock import MagicMock, patch
        import tempfile

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create real BackgroundJobManager (no mock)
            job_manager = BackgroundJobManager()

            # Create mock dependencies for RefreshScheduler
            mock_config = MagicMock()
            mock_query_tracker = MagicMock()
            mock_cleanup_manager = MagicMock()

            with patch(
                "code_indexer.server.utils.registry_factory.get_server_global_registry"
            ) as mock_get_registry:
                mock_registry = MagicMock()
                mock_get_registry.return_value = mock_registry

                scheduler = RefreshScheduler(
                    golden_repos_dir=tmpdir,
                    config_source=mock_config,
                    query_tracker=mock_query_tracker,
                    cleanup_manager=mock_cleanup_manager,
                    background_job_manager=job_manager,
                )

                # Verify that when scheduler tries to submit a job during maintenance,
                # BackgroundJobManager raises MaintenanceModeError
                with pytest.raises(MaintenanceModeError):
                    scheduler._submit_refresh_job("test-repo-global")


@pytest.mark.slow
class TestHealthEndpointMaintenanceMode:
    """Test AC6: Health endpoint includes maintenance_mode field."""

    def test_health_endpoint_includes_maintenance_mode_false(self):
        """AC6: /health response should include maintenance_mode field (false when not in maintenance)."""
        from fastapi.testclient import TestClient
        from code_indexer.server.app import create_app
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
        )
        from code_indexer.server.auth.dependencies import get_current_user

        _reset_maintenance_state()
        app = create_app()

        # Create mock user for authentication bypass
        mock_user = MagicMock()
        mock_user.username = "test_admin"
        mock_user.role = "admin"

        # Override auth dependency
        app.dependency_overrides[get_current_user] = lambda: mock_user

        client = TestClient(app)

        # Check health endpoint
        health_response = client.get("/health")
        assert health_response.status_code == 200

        data = health_response.json()
        assert "maintenance_mode" in data
        assert data["maintenance_mode"] is False

        # Clean up
        app.dependency_overrides.clear()

    def test_health_endpoint_includes_maintenance_mode_true(self):
        """AC6: /health response should include maintenance_mode field (true when in maintenance)."""
        from fastapi.testclient import TestClient
        from code_indexer.server.app import create_app
        from code_indexer.server.services.maintenance_service import (
            _reset_maintenance_state,
            get_maintenance_state,
        )
        from code_indexer.server.auth.dependencies import get_current_user

        _reset_maintenance_state()
        state = get_maintenance_state()
        state.enter_maintenance_mode()

        app = create_app()

        # Create mock user for authentication bypass
        mock_user = MagicMock()
        mock_user.username = "test_admin"
        mock_user.role = "admin"

        # Override auth dependency
        app.dependency_overrides[get_current_user] = lambda: mock_user

        client = TestClient(app)

        # Check health endpoint
        health_response = client.get("/health")
        assert health_response.status_code == 200

        data = health_response.json()
        assert "maintenance_mode" in data
        assert data["maintenance_mode"] is True

        # Clean up — reset singleton so downstream tests don't see maintenance mode
        _reset_maintenance_state()
        app.dependency_overrides.clear()


class TestMaintenanceStateAutouseTeardownBug1446:
    """Bug #1446: prove the autouse fixture's teardown half (the code that
    runs AFTER the yield) actually resets the singleton.

    Every test in this file already calls _reset_maintenance_state() manually
    at its own start. That reset-BEFORE alone is not enough: if a test in this
    file enters maintenance mode and never explicitly exits/resets, it leaves
    the module-level singleton dirty (is_maintenance_mode() == True) for
    whatever pytest schedules next in the same worker process. When that next
    test lives in an unrelated file (e.g.
    test_pod_pull_executing_node_bug1430.py or
    test_pod_pull_work_stealing_roundtrip_bug1424.py) with no reset of its
    own, its BackgroundJobManager.submit_job() call raises
    MaintenanceModeError even though it has nothing to do with maintenance
    mode.

    This test does NOT merely re-prove that _reset_maintenance_state() works
    (already covered by every other test above) and does NOT rely on two
    separate test functions running back-to-back in file order (which would
    be confounded by the SECOND test's own reset-BEFORE step masking a
    missing reset-AFTER step). Instead it drives the exact reset-before/
    reset-after generator the autouse fixture delegates to, directly
    simulating one full test lifecycle (setup -> dirty state -> teardown) and
    asserting cleanliness strictly because the teardown half ran.
    """

    def test_teardown_resets_state_after_test_left_it_dirty(self):
        """Advance the reset-cycle generator past setup, dirty the state
        exactly like a test that enters maintenance mode without cleaning up
        after itself, then advance the generator past its teardown half and
        confirm the singleton was reset."""
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        cycle = _maintenance_state_reset_cycle()
        next(cycle)  # runs the reset-BEFORE step, pauses at the yield

        state = get_maintenance_state()
        state.enter_maintenance_mode()
        assert state.is_maintenance_mode() is True  # test body dirtied state

        # Simulate pytest resuming the fixture generator for teardown.
        with pytest.raises(StopIteration):
            next(cycle)  # runs the reset-AFTER step

        # A fresh lookup must show a clean, non-maintenance singleton — this
        # is true ONLY because the teardown half of the generator ran.
        fresh_state = get_maintenance_state()
        assert fresh_state.is_maintenance_mode() is False
        assert fresh_state.get_status()["entered_at"] is None
