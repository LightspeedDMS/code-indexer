"""
Unit tests for Bug #371: Refinement job invokable from Dependency Map tab.

Tests:
  1. run_tracked_refinement() registers a job with the job tracker
  2. run_tracked_refinement() calls run_refinement_cycle()
  3. run_tracked_refinement() completes the job on success
  4. run_tracked_refinement() fails the job on exception
  5. Route handler returns 202 with success response
  6. Button HTML exists in the template

For service method tests: _job_tracker and run_refinement_cycle() are mocked
because we are testing the wrapper/integration, not the refinement logic
(run_refinement_cycle is already tested by Story #359).

For the route handler test: uses real FastAPI test client (anti-mock pattern).
"""

import re
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a minimal DependencyMapService with mocked internals
# ─────────────────────────────────────────────────────────────────────────────


def _make_service_with_mocks():
    """
    Build a DependencyMapService instance with mocked collaborators.

    Mocks _job_tracker and run_refinement_cycle so we can test only the
    run_tracked_refinement wrapper logic without executing real analysis.
    """
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    mock_config_manager = MagicMock()
    mock_config = MagicMock()
    mock_config.refinement_domains_per_run = 3
    mock_config_manager.get_claude_integration_config.return_value = mock_config

    mock_job_tracker = MagicMock()
    # check_operation_conflict does nothing by default (no DuplicateJobError)
    mock_job_tracker.check_operation_conflict.return_value = None
    mock_job_tracker.register_job.return_value = None
    mock_job_tracker.update_status.return_value = None
    mock_job_tracker.complete_job.return_value = None
    mock_job_tracker.fail_job.return_value = None

    service = DependencyMapService(
        golden_repos_manager=MagicMock(),
        config_manager=mock_config_manager,
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
        refresh_scheduler=None,
        job_tracker=mock_job_tracker,
    )

    # Patch run_refinement_cycle to avoid real I/O
    service.run_refinement_cycle = MagicMock(return_value=None)

    return service, mock_job_tracker


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: run_tracked_refinement() registers a job with the tracker
# ─────────────────────────────────────────────────────────────────────────────


class TestRunTrackedRefinementRegistersJob:
    """Test 1: run_tracked_refinement registers a job with _job_tracker."""

    def test_registers_job_with_operation_type_refinement(self):
        """Job must be registered with operation_type 'dependency_map_refinement'."""
        service, mock_job_tracker = _make_service_with_mocks()

        service.run_tracked_refinement("test-job-001")

        # register_job should be called with the provided job_id and correct operation type
        mock_job_tracker.register_job.assert_called_once()
        call_args = mock_job_tracker.register_job.call_args
        assert call_args[0][0] == "test-job-001", "job_id should be passed through"
        assert (
            call_args[0][1] == "dependency_map_refinement"
        ), "operation_type must be 'dependency_map_refinement'"

    def test_generates_job_id_when_none_provided(self):
        """When no job_id provided, a dep-map-refinement-XXXX id is generated."""
        service, mock_job_tracker = _make_service_with_mocks()

        service.run_tracked_refinement()

        mock_job_tracker.register_job.assert_called_once()
        registered_job_id = mock_job_tracker.register_job.call_args[0][0]
        assert registered_job_id.startswith(
            "dep-map-refinement-"
        ), f"Auto-generated job_id should start with 'dep-map-refinement-', got: {registered_job_id}"

    def test_sets_status_running_after_registration(self):
        """update_status should be called with status='running' after registration."""
        service, mock_job_tracker = _make_service_with_mocks()

        service.run_tracked_refinement("test-job-002")

        # Find any call that sets status=running
        running_calls = [
            c
            for c in mock_job_tracker.update_status.call_args_list
            if c[1].get("status") == "running"
            or (len(c[0]) > 1 and c[0][1] == "running")
        ]
        assert (
            len(running_calls) > 0
        ), "update_status should be called with status='running'"

    def test_checks_operation_conflict_before_registering(self):
        """check_operation_conflict must be called before register_job."""
        service, mock_job_tracker = _make_service_with_mocks()
        call_order = []
        mock_job_tracker.check_operation_conflict.side_effect = (
            lambda *a, **kw: call_order.append("check")
        )
        mock_job_tracker.register_job.side_effect = lambda *a, **kw: call_order.append(
            "register"
        )

        service.run_tracked_refinement("test-job-003")

        assert (
            call_order[0] == "check"
        ), "check_operation_conflict must be called before register_job"
        assert "register" in call_order


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: run_tracked_refinement() calls run_refinement_cycle()
# ─────────────────────────────────────────────────────────────────────────────


class TestRunTrackedRefinementCallsRefinementCycle:
    """Test 2: run_tracked_refinement delegates work to run_refinement_cycle."""

    def test_calls_run_refinement_cycle_once(self):
        """The wrapper must call run_refinement_cycle exactly once."""
        service, _ = _make_service_with_mocks()

        service.run_tracked_refinement("test-job-004")

        service.run_refinement_cycle.assert_called_once()

    def test_calls_run_refinement_cycle_with_no_args(self):
        """run_refinement_cycle takes no arguments."""
        service, _ = _make_service_with_mocks()

        service.run_tracked_refinement("test-job-005")

        service.run_refinement_cycle.assert_called_once_with()


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: run_tracked_refinement() completes the job on success
# ─────────────────────────────────────────────────────────────────────────────


class TestRunTrackedRefinementCompletesJobOnSuccess:
    """Test 3: complete_job is called when run_refinement_cycle succeeds."""

    def test_complete_job_called_on_success(self):
        """complete_job must be called after successful run_refinement_cycle."""
        service, mock_job_tracker = _make_service_with_mocks()

        service.run_tracked_refinement("test-job-006")

        mock_job_tracker.complete_job.assert_called_once()
        completed_job_id = mock_job_tracker.complete_job.call_args[0][0]
        assert completed_job_id == "test-job-006"

    def test_fail_job_not_called_on_success(self):
        """fail_job must NOT be called when run_refinement_cycle succeeds."""
        service, mock_job_tracker = _make_service_with_mocks()

        service.run_tracked_refinement("test-job-007")

        mock_job_tracker.fail_job.assert_not_called()

    def test_returns_result_dict_on_success(self):
        """run_tracked_refinement should return a dict with status='completed'."""
        service, _ = _make_service_with_mocks()

        result = service.run_tracked_refinement("test-job-008")

        assert isinstance(result, dict), "Result must be a dict"
        assert result.get("status") == "completed"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: run_tracked_refinement() fails the job on exception
# ─────────────────────────────────────────────────────────────────────────────


class TestRunTrackedRefinementFailsJobOnException:
    """Test 4: fail_job is called when run_refinement_cycle raises an exception."""

    def test_fail_job_called_when_refinement_raises(self):
        """fail_job must be called if run_refinement_cycle raises an exception."""
        service, mock_job_tracker = _make_service_with_mocks()
        service.run_refinement_cycle.side_effect = RuntimeError("Claude CLI failed")

        with pytest.raises(RuntimeError, match="Claude CLI failed"):
            service.run_tracked_refinement("test-job-009")

        mock_job_tracker.fail_job.assert_called_once()
        call_args = mock_job_tracker.fail_job.call_args
        assert (
            call_args[0][0] == "test-job-009"
        ), "fail_job must receive the correct job_id"

    def test_complete_job_not_called_on_exception(self):
        """complete_job must NOT be called when run_refinement_cycle raises."""
        service, mock_job_tracker = _make_service_with_mocks()
        service.run_refinement_cycle.side_effect = ValueError("Unexpected error")

        with pytest.raises(ValueError):
            service.run_tracked_refinement("test-job-010")

        mock_job_tracker.complete_job.assert_not_called()

    def test_exception_propagates_to_caller(self):
        """The original exception must propagate (no swallowing)."""
        service, _ = _make_service_with_mocks()
        service.run_refinement_cycle.side_effect = RuntimeError("propagation check")

        with pytest.raises(RuntimeError, match="propagation check"):
            service.run_tracked_refinement("test-job-011")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures for route handler tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Create FastAPI app with minimal startup."""
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie via form-based login."""
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    login_resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies, "No session cookie set by form login"
    return login_resp.cookies


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Route handler returns 202 with success response
# ─────────────────────────────────────────────────────────────────────────────


class TestTriggerRefinementRoute:
    """Test 5: POST /admin/dependency-map/trigger-refinement returns 202."""

    def test_returns_202_when_service_available(self, client, admin_session_cookie):
        """Route returns 202 Accepted when dep_map_service is available and idle."""
        from code_indexer.server.app import app

        mock_service = MagicMock()
        mock_service.is_available.return_value = True
        mock_service.run_tracked_refinement = MagicMock(
            return_value={"status": "completed"}
        )

        original_service = getattr(app.state, "dependency_map_service", None)
        app.state.dependency_map_service = mock_service
        try:
            response = client.post(
                "/admin/dependency-map/trigger-refinement",
                cookies=admin_session_cookie,
            )
            assert (
                response.status_code == 202
            ), f"Expected 202, got {response.status_code}: {response.text}"
            body = response.json()
            assert body.get("success") is True
            assert "job_id" in body
        finally:
            app.state.dependency_map_service = original_service

    def test_returns_401_without_auth(self, client):
        """Route returns 401 when not authenticated."""
        response = client.post("/admin/dependency-map/trigger-refinement")
        assert response.status_code == 401

    def test_returns_503_when_service_unavailable(self, client, admin_session_cookie):
        """Route returns 503 when dep_map_service is None."""
        from code_indexer.server.app import app

        original_service = getattr(app.state, "dependency_map_service", None)
        app.state.dependency_map_service = None
        try:
            response = client.post(
                "/admin/dependency-map/trigger-refinement",
                cookies=admin_session_cookie,
            )
            assert response.status_code == 503
        finally:
            app.state.dependency_map_service = original_service

    def test_returns_409_when_analysis_in_progress(self, client, admin_session_cookie):
        """Route returns 409 when service is_available() returns False."""
        from code_indexer.server.app import app

        mock_service = MagicMock()
        mock_service.is_available.return_value = False

        original_service = getattr(app.state, "dependency_map_service", None)
        app.state.dependency_map_service = mock_service
        try:
            response = client.post(
                "/admin/dependency-map/trigger-refinement",
                cookies=admin_session_cookie,
            )
            assert response.status_code == 409
        finally:
            app.state.dependency_map_service = original_service

    def test_job_id_has_refinement_prefix(self, client, admin_session_cookie):
        """Returned job_id must start with 'dep-map-refinement-'."""
        from code_indexer.server.app import app

        mock_service = MagicMock()
        mock_service.is_available.return_value = True
        mock_service.run_tracked_refinement = MagicMock(
            return_value={"status": "completed"}
        )

        original_service = getattr(app.state, "dependency_map_service", None)
        app.state.dependency_map_service = mock_service
        try:
            response = client.post(
                "/admin/dependency-map/trigger-refinement",
                cookies=admin_session_cookie,
            )
            assert response.status_code == 202
            body = response.json()
            assert body["job_id"].startswith(
                "dep-map-refinement-"
            ), f"job_id should start with 'dep-map-refinement-', got: {body['job_id']}"
        finally:
            app.state.dependency_map_service = original_service


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Button HTML exists in the template
# ─────────────────────────────────────────────────────────────────────────────


class TestRefinementButtonInTemplate:
    """Test 6: 'Refinement Pass' button exists in depmap_job_status.html."""

    def test_refinement_button_in_template_file(self):
        """The template must contain the Refinement Pass button."""
        from pathlib import Path

        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "partials"
            / "depmap_job_status.html"
        )
        assert template_path.exists(), f"Template file not found: {template_path}"
        content = template_path.read_text()
        assert (
            "trigger-refinement" in content
        ), "Template must contain 'trigger-refinement' (button HTMX post target)"
        assert (
            "Refinement Pass" in content
        ), "Template must contain 'Refinement Pass' button text"

    def test_refinement_button_uses_correct_endpoint(self):
        """Button must post to /admin/dependency-map/trigger-refinement."""
        from pathlib import Path

        template_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
            / "partials"
            / "depmap_job_status.html"
        )
        content = template_path.read_text()
        assert (
            "/admin/dependency-map/trigger-refinement" in content
        ), "Button must use /admin/dependency-map/trigger-refinement as HTMX post target"
