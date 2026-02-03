"""
Unit tests for Bug #134: Cancel Job CSRF Token Issue.

Tests verify that cancel_job endpoint:
1. Works without CSRF token when valid admin session exists
2. Still requires valid admin session authentication

These tests are written FIRST following TDD methodology.
"""

from unittest.mock import MagicMock, patch
from fastapi import Request
from fastapi.responses import HTMLResponse


class TestCancelJobCSRFRemoval:
    """Tests for Bug #134: CSRF removal from cancel_job endpoint."""

    def test_cancel_job_works_without_csrf_token(self):
        """
        Bug #134: cancel_job should work without CSRF token.

        Given a valid admin session
        When cancel_job is called WITHOUT a CSRF token
        Then the job should be cancelled successfully
        """
        from src.code_indexer.server.web.routes import cancel_job

        # Mock request with valid admin session
        mock_request = MagicMock(spec=Request)
        mock_request.cookies = {}

        # Mock session data (valid admin)
        mock_session = MagicMock()
        mock_session.username = "admin"
        mock_session.role = "admin"

        # Mock job manager with successful cancel
        mock_job_manager = MagicMock()
        mock_job_manager.cancel_job.return_value = {
            "success": True,
            "message": "Job cancelled successfully",
        }

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_background_job_manager",
                return_value=mock_job_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_jobs_page_response"
            ) as mock_response,
        ):
            mock_response.return_value = HTMLResponse(content="<html>Success</html>")

            # Call cancel_job (no CSRF token parameter needed)
            result = cancel_job(
                request=mock_request,
                job_id="test-job-123",
            )

            # Should succeed without CSRF validation
            assert result is not None
            mock_job_manager.cancel_job.assert_called_once_with("test-job-123", "admin")
            mock_response.assert_called_once()
            # Verify success_message passed (not error_message)
            call_kwargs = mock_response.call_args[1]
            assert "success_message" in call_kwargs

    def test_cancel_job_still_requires_admin_session(self):
        """
        Bug #134: cancel_job should still require valid admin session.

        Given NO valid admin session
        When cancel_job is called
        Then authentication should fail and redirect to login
        """
        from src.code_indexer.server.web.routes import cancel_job

        # Mock request with NO valid session
        mock_request = MagicMock(spec=Request)
        mock_request.cookies = {}

        # Mock _require_admin_session returns None (not authenticated)
        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=None,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_login_redirect"
            ) as mock_redirect,
        ):
            mock_redirect.return_value = HTMLResponse(
                content="<html>Redirect to login</html>"
            )

            # Call cancel_job
            result = cancel_job(
                request=mock_request,
                job_id="test-job-123",
            )

            # Should redirect to login (authentication failed)
            assert result is not None
            mock_redirect.assert_called_once_with(mock_request)
