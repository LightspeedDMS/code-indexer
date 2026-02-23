"""
Unit tests for Story #272: Force Re-sync web route.

Tests:
1. POST /admin/golden-repos/{alias}/force-resync requires admin session
2. POST /admin/golden-repos/{alias}/force-resync validates CSRF token
3. Successful force re-sync calls trigger_refresh_for_repo(force_reset=True)
4. Success response includes job ID
5. Error when repo not found
6. Error when RefreshScheduler not available
"""

from unittest.mock import MagicMock, patch
from fastapi import Request
from fastapi.responses import HTMLResponse


def _make_request():
    """Create a mock request with cookies dict."""
    mock_request = MagicMock(spec=Request)
    mock_request.cookies = {}
    return mock_request


def _make_session(username="admin"):
    """Create a mock admin session."""
    mock_session = MagicMock()
    mock_session.username = username
    mock_session.role = "admin"
    return mock_session


class TestForceResyncRoute:
    """Tests for POST /admin/golden-repos/{alias}/force-resync endpoint."""

    def test_force_resync_requires_admin_session(self):
        """
        AC8: Force re-sync endpoint must require valid admin session.
        When no session, redirect to login.
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=None,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_login_redirect"
            ) as mock_redirect,
        ):
            mock_redirect.return_value = HTMLResponse(content="", status_code=401)

            result = force_resync_golden_repo(
                request=mock_request,
                alias="my-repo",
                csrf_token="valid-token",
            )

        # Should redirect (not proceed with force resync)
        assert result is not None
        mock_redirect.assert_called_once_with(mock_request)

    def test_force_resync_rejects_missing_csrf_token(self):
        """
        AC8: CSRF protection on force re-sync endpoint.
        POST with missing CSRF token must be rejected with error page.
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()
        mock_session = _make_session()

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=False,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response"
            ) as mock_page,
        ):
            mock_page.return_value = HTMLResponse(content="<html>error</html>")

            force_resync_golden_repo(
                request=mock_request,
                alias="my-repo",
                csrf_token=None,  # Missing CSRF token
            )

        # Should return error page (CSRF rejected)
        assert mock_page.called
        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "error_message" in call_kwargs
        assert call_kwargs["error_message"] is not None

    def test_force_resync_rejects_invalid_csrf_token(self):
        """
        AC8: POST with invalid CSRF token must be rejected.
        No force re-sync operation triggered (verified by scheduler not called).
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()
        mock_session = _make_session()

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo.return_value = "job-id"

        mock_manager = MagicMock()
        mock_manager.golden_repos = {"my-repo": {}}

        mock_lifecycle = MagicMock()
        mock_lifecycle.refresh_scheduler = mock_scheduler

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=False,  # Invalid CSRF
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response"
            ) as mock_page,
        ):
            import code_indexer.server.app as app_module
            original_state = app_module.app.state
            app_module.app.state = MagicMock()
            app_module.app.state.global_lifecycle_manager = mock_lifecycle
            try:
                mock_page.return_value = HTMLResponse(content="<html>error</html>")
                force_resync_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="bad-token",
                )
            finally:
                app_module.app.state = original_state

        # Scheduler must NOT have been called (CSRF rejected before reaching scheduler)
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_force_resync_success_calls_trigger_with_force_reset(self):
        """
        AC3: Successful force re-sync must call
        trigger_refresh_for_repo(alias, force_reset=True).
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()
        mock_session = _make_session(username="admin")

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo.return_value = "job-id-456"

        mock_manager = MagicMock()
        mock_manager.golden_repos = {"my-repo": {}}

        mock_lifecycle = MagicMock()
        mock_lifecycle.refresh_scheduler = mock_scheduler

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response"
            ) as mock_page,
        ):
            import code_indexer.server.app as app_module
            original_state = app_module.app.state
            app_module.app.state = MagicMock()
            app_module.app.state.global_lifecycle_manager = mock_lifecycle
            try:
                mock_page.return_value = HTMLResponse(content="<html>success</html>")
                force_resync_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        # Must have called trigger_refresh_for_repo with force_reset=True
        mock_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "my-repo",
            submitter_username="admin",
            force_reset=True,
        )

    def test_force_resync_success_response_includes_job_id(self):
        """
        AC3: Success response must include job ID.
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()
        mock_session = _make_session(username="admin")

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo.return_value = "job-id-789"

        mock_manager = MagicMock()
        mock_manager.golden_repos = {"my-repo": {}}

        mock_lifecycle = MagicMock()
        mock_lifecycle.refresh_scheduler = mock_scheduler

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response"
            ) as mock_page,
        ):
            import code_indexer.server.app as app_module
            original_state = app_module.app.state
            app_module.app.state = MagicMock()
            app_module.app.state.global_lifecycle_manager = mock_lifecycle
            try:
                mock_page.return_value = HTMLResponse(content="<html>success</html>")
                force_resync_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        # Success message must include job ID
        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "success_message" in call_kwargs
        assert "job-id-789" in call_kwargs["success_message"]

    def test_force_resync_error_when_repo_not_found(self):
        """
        AC3: Error response when repo not found in golden_repos.
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()
        mock_session = _make_session()

        mock_manager = MagicMock()
        mock_manager.golden_repos = {}  # Repo not in dict

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response"
            ) as mock_page,
        ):
            mock_page.return_value = HTMLResponse(content="<html>error</html>")

            force_resync_golden_repo(
                request=mock_request,
                alias="nonexistent-repo",
                csrf_token="valid-token",
            )

        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "error_message" in call_kwargs
        assert call_kwargs["error_message"] is not None

    def test_force_resync_error_when_scheduler_not_available(self):
        """
        AC3: Error response when RefreshScheduler is not available.
        """
        from src.code_indexer.server.web.routes import force_resync_golden_repo

        mock_request = _make_request()
        mock_session = _make_session()

        mock_manager = MagicMock()
        mock_manager.golden_repos = {"my-repo": {}}

        mock_lifecycle = MagicMock()
        mock_lifecycle.refresh_scheduler = None  # Scheduler not available

        with (
            patch(
                "src.code_indexer.server.web.routes._require_admin_session",
                return_value=mock_session,
            ),
            patch(
                "src.code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "src.code_indexer.server.web.routes._get_golden_repo_manager",
                return_value=mock_manager,
            ),
            patch(
                "src.code_indexer.server.web.routes._create_golden_repos_page_response"
            ) as mock_page,
        ):
            import code_indexer.server.app as app_module
            original_state = app_module.app.state
            app_module.app.state = MagicMock()
            app_module.app.state.global_lifecycle_manager = mock_lifecycle
            try:
                mock_page.return_value = HTMLResponse(content="<html>error</html>")
                force_resync_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "error_message" in call_kwargs
        assert call_kwargs["error_message"] is not None
