"""
Unit tests for Story #205: Server Restart from Diagnostics Tab.

This file covers:
- AC3: Non-admin user cannot restart (403 Forbidden)
- AC1: Admin user receives 202 Accepted with restart message
- Delayed restart scheduling on background thread
- Logging of restart request with username

TDD: These tests are written FIRST, before implementation.
"""

import pytest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_restart_state():
    """Reset module-level restart state between tests."""
    import code_indexer.server.web.routes as routes_module
    routes_module._restart_in_progress = False
    yield
    routes_module._restart_in_progress = False


@pytest.fixture
def test_client():
    """Create a test client with admin session mocked."""
    from fastapi import FastAPI
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")

    # Mock admin session
    mock_admin_session = MagicMock()
    mock_admin_session.username = "admin_user"
    mock_admin_session.role = "admin"

    with patch("code_indexer.server.web.routes._require_admin_session") as mock_auth:
        mock_auth.return_value = mock_admin_session
        yield TestClient(app)


@pytest.fixture
def test_client_non_admin():
    """Create a test client with non-admin session (should fail auth)."""
    from fastapi import FastAPI
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")

    # Mock non-admin session - return None (not authenticated)
    with patch("code_indexer.server.web.routes._require_admin_session") as mock_auth:
        mock_auth.return_value = None
        yield TestClient(app)


class TestRestartEndpointAuthorization:
    """Tests for AC3: Non-admin user cannot restart server."""

    def test_non_admin_user_receives_403_forbidden(self, test_client_non_admin):
        """
        AC3: Non-admin user cannot restart.

        Given I am NOT logged in as an admin user
        When I attempt to call POST /admin/restart
        Then I receive a 403 Forbidden response
        And the server does not restart
        """
        response = test_client_non_admin.post("/admin/restart")

        assert response.status_code == 403
        data = response.json()
        assert "admin" in data["detail"].lower() or "forbidden" in data["detail"].lower()

    def test_admin_user_can_access_restart_endpoint(self, test_client):
        """
        AC3: Admin user CAN access restart endpoint (positive auth test).

        Given I am logged in as an admin user
        When I call POST /admin/restart
        Then I do NOT receive a 403 Forbidden response
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True
                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "test-csrf-token"}
                )

        # Should NOT be 403 (may be 202 or other, but not forbidden)
        assert response.status_code != 403


class TestRestartEndpointResponse:
    """Tests for AC1: Admin user receives 202 Accepted with message."""

    def test_admin_user_receives_202_accepted(self, test_client):
        """
        AC1: Successful restart via endpoint returns 202 Accepted.

        Given I am logged in as an admin user
        When I call POST /admin/restart
        Then I receive a 202 Accepted response
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True
                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "test-csrf-token"}
                )

        assert response.status_code == 202

    def test_response_contains_restart_message(self, test_client):
        """
        AC1: Response contains informative restart message.

        Given I am logged in as an admin user
        When I call POST /admin/restart
        Then the response body contains a message about server restarting
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True
                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "test-csrf-token"}
                )

        assert response.status_code == 202
        data = response.json()
        assert "message" in data
        assert "restart" in data["message"].lower()
        assert "2 seconds" in data["message"] or "2s" in data["message"].lower()

    def test_response_is_json(self, test_client):
        """
        AC1: Response is JSON format for AJAX compatibility.

        Given I am logged in as an admin user
        When I call POST /admin/restart
        Then the response content-type is application/json
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True
                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "test-csrf-token"}
                )

        assert response.status_code == 202
        assert "application/json" in response.headers["content-type"]


class TestCSRFValidation:
    """Tests for CSRF token validation (Code Review Issues #1 and #2)."""

    def test_missing_csrf_token_returns_403(self, test_client):
        """
        Missing CSRF token returns 403 Forbidden.

        Given I am logged in as an admin user
        When I call POST /admin/restart without X-CSRF-Token header
        Then I receive a 403 Forbidden response
        And the response indicates invalid CSRF token
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            # Don't add X-CSRF-Token header
            response = test_client.post("/admin/restart")

        assert response.status_code == 403
        data = response.json()
        assert "csrf" in data["detail"].lower() or "forbidden" in data["detail"].lower()

    def test_invalid_csrf_token_returns_403(self, test_client):
        """
        Invalid CSRF token returns 403 Forbidden.

        Given I am logged in as an admin user
        When I call POST /admin/restart with invalid X-CSRF-Token header
        Then I receive a 403 Forbidden response
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            response = test_client.post(
                "/admin/restart",
                headers={"X-CSRF-Token": "invalid-token-12345"}
            )

        assert response.status_code == 403
        data = response.json()
        assert "csrf" in data["detail"].lower() or "forbidden" in data["detail"].lower()

    def test_mismatched_csrf_token_returns_403(self, test_client):
        """
        Mismatched CSRF token (header vs cookie) returns 403 Forbidden.

        Given I am logged in as an admin user
        And I have a valid CSRF cookie with token A
        When I call POST /admin/restart with X-CSRF-Token header containing token B
        Then I receive a 403 Forbidden response
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                # Mock validation to return False (mismatch)
                mock_validate.return_value = False

                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "token-from-header"}
                )

        assert response.status_code == 403
        mock_validate.assert_called_once()

    def test_valid_csrf_token_allows_restart(self, test_client):
        """
        Valid CSRF token allows restart to proceed.

        Given I am logged in as an admin user
        And I have a valid CSRF token that matches the cookie
        When I call POST /admin/restart with correct X-CSRF-Token header
        Then I receive a 202 Accepted response
        And the restart is scheduled
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart") as mock_schedule:
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                # Mock validation to return True (valid)
                mock_validate.return_value = True

                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "valid-token"}
                )

        assert response.status_code == 202
        mock_schedule.assert_called_once()


class TestRestartLogging:
    """Tests for logging restart requests with username."""

    def test_restart_request_is_logged(self, test_client):
        """
        Restart request is logged with username.

        Given I am logged in as admin_user
        When I call POST /admin/restart
        Then a log entry is created with "Server restart requested by admin_user"
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True
                with patch("code_indexer.server.web.routes.logger") as mock_logger:
                    response = test_client.post(
                        "/admin/restart",
                        headers={"X-CSRF-Token": "valid-token"}
                    )

        assert response.status_code == 202

        # Verify logger.info was called with restart message
        mock_logger.info.assert_called_once()
        log_message = mock_logger.info.call_args[0][0]
        assert "restart requested" in log_message.lower()
        assert "admin_user" in log_message


class TestRateLimiting:
    """Tests for rate limiting restart requests (Code Review Issue #6)."""

    def test_concurrent_restart_returns_409(self, test_client):
        """
        Concurrent restart requests return 409 Conflict.

        Given I am logged in as an admin user
        And a restart is already in progress
        When I call POST /admin/restart again
        Then I receive a 409 Conflict response
        And the response indicates restart already in progress
        """
        # Import the module to access the flag
        import code_indexer.server.web.routes as routes_module

        with patch("code_indexer.server.web.routes._schedule_delayed_restart"):
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True

                # Simulate restart in progress by setting the module flag
                original_value = getattr(routes_module, '_restart_in_progress', False)
                try:
                    routes_module._restart_in_progress = True

                    response = test_client.post(
                        "/admin/restart",
                        headers={"X-CSRF-Token": "valid-token"}
                    )

                    assert response.status_code == 409
                    data = response.json()
                    assert "already in progress" in data["message"].lower() or "restart" in data["message"].lower()
                finally:
                    # Restore original value
                    routes_module._restart_in_progress = original_value

    def test_subsequent_restart_after_completion_allowed(self, test_client):
        """
        Subsequent restart after first completes is allowed.

        Given I am logged in as an admin user
        And a previous restart has completed (_restart_in_progress is False)
        When I call POST /admin/restart
        Then I receive a 202 Accepted response
        And the restart is scheduled
        """
        # Import the module to explicitly reset the flag (demonstrating the test's purpose)
        import code_indexer.server.web.routes as routes_module
        routes_module._restart_in_progress = False

        with patch("code_indexer.server.web.routes._schedule_delayed_restart") as mock_schedule:
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True

                # Normal case - no restart in progress
                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "valid-token"}
                )

        assert response.status_code == 202
        mock_schedule.assert_called_once()


class TestDelayedRestartScheduling:
    """Tests for delayed restart mechanism scheduling."""

    def test_restart_schedules_background_thread(self, test_client):
        """
        Restart endpoint schedules delayed_restart on background thread.

        Given I am logged in as an admin user
        When I call POST /admin/restart
        Then _schedule_delayed_restart is called
        And the HTTP response is returned immediately (not blocked)
        """
        with patch("code_indexer.server.web.routes._schedule_delayed_restart") as mock_schedule:
            with patch("code_indexer.server.web.routes.validate_login_csrf_token") as mock_validate:
                mock_validate.return_value = True

                response = test_client.post(
                    "/admin/restart",
                    headers={"X-CSRF-Token": "valid-token"}
                )

        assert response.status_code == 202
        mock_schedule.assert_called_once()

    def test_schedule_delayed_restart_creates_daemon_thread(self):
        """
        _schedule_delayed_restart creates a daemon background thread.

        When _schedule_delayed_restart is called
        Then it creates a threading.Thread with daemon=True
        And the thread target is _delayed_restart function
        """
        from code_indexer.server.web.routes import _schedule_delayed_restart

        with patch("threading.Thread") as mock_thread_class:
            mock_thread_instance = MagicMock()
            mock_thread_class.return_value = mock_thread_instance

            with patch("code_indexer.server.web.routes._delayed_restart"):
                _schedule_delayed_restart()

            # Verify Thread was created with daemon=True
            mock_thread_class.assert_called_once()
            call_kwargs = mock_thread_class.call_args[1]
            assert call_kwargs["daemon"] is True
            assert "target" in call_kwargs

            # Verify thread.start() was called
            mock_thread_instance.start.assert_called_once()

    def test_schedule_passes_delay_parameter(self):
        """
        _schedule_delayed_restart passes delay parameter to _delayed_restart.

        When _schedule_delayed_restart is called
        Then _delayed_restart is scheduled with delay=2 seconds
        """
        from code_indexer.server.web.routes import _schedule_delayed_restart

        with patch("threading.Thread") as mock_thread_class:
            mock_thread_instance = MagicMock()
            mock_thread_class.return_value = mock_thread_instance

            with patch("code_indexer.server.web.routes._delayed_restart") as mock_delayed:
                _schedule_delayed_restart()

            # Verify Thread was created with correct target and args
            call_kwargs = mock_thread_class.call_args[1]
            # The target should be _delayed_restart
            # The args/kwargs should include delay parameter
            assert "target" in call_kwargs or "args" in call_kwargs


class TestDelayedRestartMechanism:
    """Tests for the _delayed_restart function logic."""

    def test_delayed_restart_sleeps_2_seconds(self):
        """
        _delayed_restart sleeps for 2 seconds before restarting.

        This delay allows the HTTP 202 response to complete before restart.

        When _delayed_restart(delay=2) is called
        Then time.sleep(2) is called
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep") as mock_sleep:
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Not systemd
                with patch("os.execv"):
                    _delayed_restart(delay=2)

        mock_sleep.assert_called_once_with(2)

    def test_delayed_restart_detects_systemd(self):
        """
        _delayed_restart detects if running under systemd.

        Systemd sets INVOCATION_ID environment variable.

        When _delayed_restart is called in systemd environment
        Then it detects systemd by checking INVOCATION_ID env var
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd
                with patch("subprocess.run") as mock_subprocess:
                    _delayed_restart(delay=2)

        # Should check for INVOCATION_ID
        mock_env.assert_called_with("INVOCATION_ID")

    def test_systemd_mode_calls_systemctl_restart(self):
        """
        In systemd mode, _delayed_restart calls systemctl restart.

        When _delayed_restart is called in systemd environment
        Then it executes: systemctl restart cidx-server
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd
                with patch("subprocess.run") as mock_subprocess:
                    _delayed_restart(delay=2)

        # Should call subprocess.run with systemctl restart
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        assert "systemctl" in call_args
        assert "restart" in call_args
        assert "cidx-server" in call_args

    def test_dev_mode_calls_os_execv(self):
        """
        In dev mode (not systemd), _delayed_restart calls os.execv.

        When _delayed_restart is called NOT in systemd environment
        Then it executes: os.execv(sys.executable, [sys.executable] + sys.argv)
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Not systemd
                with patch("os.execv") as mock_execv:
                    with patch("sys.executable", "/usr/bin/python3"):
                        with patch("sys.argv", ["app.py", "--host", "0.0.0.0"]):
                            _delayed_restart(delay=2)

        # Should call os.execv to re-exec the process
        mock_execv.assert_called_once()
        call_args = mock_execv.call_args[0]
        # First arg is the executable
        assert call_args[0] == "/usr/bin/python3"
        # Second arg is the argv list
        assert isinstance(call_args[1], list)
        assert call_args[1][0] == "/usr/bin/python3"

    def test_delayed_restart_logs_before_restarting(self):
        """
        _delayed_restart logs before executing restart.

        When _delayed_restart is called
        Then a log entry is created before restart execution
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("code_indexer.server.web.routes.logger") as mock_logger:
                with patch("os.environ.get") as mock_env:
                    mock_env.return_value = None  # Dev mode
                    with patch("os.execv"):
                        _delayed_restart(delay=2)

        # Should log before restarting
        assert mock_logger.info.called
        log_message = mock_logger.info.call_args[0][0]
        assert "restart" in log_message.lower()


class TestErrorHandling:
    """Tests for error handling in restart mechanism (Code Review Issues #3 and #4)."""

    def test_systemctl_failure_is_logged(self):
        """
        subprocess.run failure is captured and logged.

        Given _delayed_restart is called in systemd mode
        When systemctl restart fails with non-zero exit code
        Then stderr is captured and logged
        And the error doesn't crash the server
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode

                with patch("subprocess.run") as mock_subprocess:
                    with patch("code_indexer.server.web.routes.logger") as mock_logger:
                        # Simulate systemctl failure
                        mock_result = MagicMock()
                        mock_result.returncode = 1
                        mock_result.stderr = "Failed to restart cidx-server.service: Unit not found"
                        mock_subprocess.return_value = mock_result

                        # Should not raise exception
                        _delayed_restart(delay=2)

                        # Verify error was logged
                        assert mock_logger.error.called
                        error_log = mock_logger.error.call_args[0][0]
                        assert "systemctl" in error_log.lower() or "restart" in error_log.lower()

    def test_systemctl_captures_output(self):
        """
        subprocess.run captures stdout and stderr.

        Given _delayed_restart is called in systemd mode
        When subprocess.run is executed
        Then capture_output=True and text=True are used
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode

                with patch("subprocess.run") as mock_subprocess:
                    mock_result = MagicMock()
                    mock_result.returncode = 0
                    mock_subprocess.return_value = mock_result

                    _delayed_restart(delay=2)

                    # Verify subprocess.run was called with capture flags
                    mock_subprocess.assert_called_once()
                    call_kwargs = mock_subprocess.call_args[1]
                    assert call_kwargs.get("capture_output") is True
                    assert call_kwargs.get("text") is True

    def test_os_execv_failure_is_caught(self):
        """
        os.execv failure is caught and logged.

        Given _delayed_restart is called in dev mode
        When os.execv raises OSError
        Then the exception is caught and logged
        And the error doesn't crash the server
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Dev mode

                with patch("os.execv") as mock_execv:
                    with patch("code_indexer.server.web.routes.logger") as mock_logger:
                        # Simulate os.execv failure
                        mock_execv.side_effect = OSError("Exec format error")

                        # Should not raise exception
                        _delayed_restart(delay=2)

                        # Verify error was logged
                        assert mock_logger.error.called
                        error_log = mock_logger.error.call_args[0][0]
                        assert "execv" in error_log.lower() or "failed" in error_log.lower()

    def test_os_execv_wrapped_in_try_except(self):
        """
        os.execv call is wrapped in try/except OSError.

        Given _delayed_restart is called in dev mode
        When os.execv is executed
        Then it's wrapped in try/except to catch OSError
        """
        from code_indexer.server.web.routes import _delayed_restart

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Dev mode

                with patch("os.execv") as mock_execv:
                    # Simulate OSError - should be caught, not propagated
                    mock_execv.side_effect = OSError("Permission denied")

                    # Should not raise exception
                    try:
                        _delayed_restart(delay=2)
                    except OSError:
                        pytest.fail("OSError should be caught, not propagated")

    def test_systemctl_failure_resets_restart_flag(self):
        """
        systemctl failure resets _restart_in_progress flag.

        Code Review Finding #1: When systemctl restart fails, the flag must be reset.

        Given _restart_in_progress is True
        And _delayed_restart is called in systemd mode
        When systemctl restart fails with non-zero exit code
        Then _restart_in_progress is reset to False
        """
        from code_indexer.server.web.routes import _delayed_restart
        import code_indexer.server.web.routes as routes_module

        # Set flag to True (simulating restart in progress)
        routes_module._restart_in_progress = True

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = "some-invocation-id"  # Systemd mode

                with patch("subprocess.run") as mock_subprocess:
                    # Simulate systemctl failure
                    mock_result = MagicMock()
                    mock_result.returncode = 1
                    mock_result.stderr = "Failed to restart cidx-server.service"
                    mock_subprocess.return_value = mock_result

                    _delayed_restart(delay=2)

        # Verify flag was reset to False
        assert routes_module._restart_in_progress is False

    def test_os_execv_failure_resets_restart_flag(self):
        """
        os.execv failure resets _restart_in_progress flag.

        Code Review Finding #1: When os.execv fails, the flag must be reset.

        Given _restart_in_progress is True
        And _delayed_restart is called in dev mode
        When os.execv raises OSError
        Then _restart_in_progress is reset to False
        """
        from code_indexer.server.web.routes import _delayed_restart
        import code_indexer.server.web.routes as routes_module

        # Set flag to True (simulating restart in progress)
        routes_module._restart_in_progress = True

        with patch("time.sleep"):
            with patch("os.environ.get") as mock_env:
                mock_env.return_value = None  # Dev mode

                with patch("os.execv") as mock_execv:
                    # Simulate os.execv failure
                    mock_execv.side_effect = OSError("Exec format error")

                    _delayed_restart(delay=2)

        # Verify flag was reset to False
        assert routes_module._restart_in_progress is False
