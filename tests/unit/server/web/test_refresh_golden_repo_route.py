"""
Unit tests for POST /golden-repos/{alias}/refresh web route.

Mirrors tests/unit/server/web/test_force_resync_route.py's structure and
fixture approach, targeting `refresh_golden_repo` instead of
`force_resync_golden_repo`.

Tests:
1. Requires admin session (401 when absent)
2. Validates CSRF token (rejects missing/invalid)
3. Successful refresh calls trigger_refresh_for_repo
4. Success response includes job ID
5. Error when repo not found
6. Error when RefreshScheduler not available
7. Bug #1481: cross-node repo (visible via get_golden_repo(), absent from
   this worker's per-process `golden_repos` cache dict) still succeeds.
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


class TestRefreshGoldenRepoRoute:
    """Tests for POST /golden-repos/{alias}/refresh endpoint."""

    def test_refresh_requires_admin_session(self):
        """Refresh endpoint must require a valid admin session -- 401 when absent."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

        mock_request = _make_request()

        with patch(
            "src.code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            result = refresh_golden_repo(
                request=mock_request,
                alias="my-repo",
                csrf_token="valid-token",
            )

        assert result is not None
        assert result.status_code == 401

    def test_refresh_rejects_missing_csrf_token(self):
        """CSRF protection on refresh endpoint: missing token must be rejected."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

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

            refresh_golden_repo(
                request=mock_request,
                alias="my-repo",
                csrf_token=None,
            )

        assert mock_page.called
        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "error_message" in call_kwargs
        assert call_kwargs["error_message"] is not None

    def test_refresh_rejects_invalid_csrf_token(self):
        """Invalid CSRF token must be rejected before reaching the scheduler."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

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
                return_value=False,
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
                refresh_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="bad-token",
                )
            finally:
                app_module.app.state = original_state

        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_refresh_success_calls_trigger_refresh_for_repo(self):
        """Successful refresh must call trigger_refresh_for_repo(alias, submitter_username=...)."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

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
                refresh_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        mock_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "my-repo",
            submitter_username="admin",
        )

    def test_refresh_success_response_includes_job_id(self):
        """Success response must include job ID."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

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
                refresh_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "success_message" in call_kwargs
        assert "job-id-789" in call_kwargs["success_message"]

    def test_refresh_error_when_repo_not_found(self):
        """Error response when repo genuinely absent (both golden_repos AND
        get_golden_repo() report no such repo)."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

        mock_request = _make_request()
        mock_session = _make_session()

        mock_manager = MagicMock()
        mock_manager.golden_repos = {}
        mock_manager.get_golden_repo.return_value = None

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

            refresh_golden_repo(
                request=mock_request,
                alias="nonexistent-repo",
                csrf_token="valid-token",
            )

        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "error_message" in call_kwargs
        assert call_kwargs["error_message"] is not None

    def test_refresh_cross_node_repo_not_cached_locally_still_succeeds_bug1481(self):
        """Bug #1481: alias exists in the shared backend (registered on
        another node) but this worker's per-process `golden_repos` cache
        dict never loaded it. The not-found gate must consult the
        authoritative `get_golden_repo()` read, not the raw per-worker
        cache dict.
        """
        from src.code_indexer.server.web.routes import refresh_golden_repo

        mock_request = _make_request()
        mock_session = _make_session(username="admin")

        mock_scheduler = MagicMock()
        mock_scheduler.trigger_refresh_for_repo.return_value = "job-id-cross-node"

        mock_manager = MagicMock()
        mock_manager.golden_repos = {}  # cold cache on this worker

        def _get_golden_repo_side_effect(alias):
            return {"alias": "mock-test"} if alias == "mock-test" else None

        mock_manager.get_golden_repo.side_effect = _get_golden_repo_side_effect

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
                refresh_golden_repo(
                    request=mock_request,
                    alias="mock-test",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        mock_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "mock-test",
            submitter_username="admin",
        )
        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "success_message" in call_kwargs
        assert "job-id-cross-node" in call_kwargs["success_message"]

    def test_refresh_error_when_scheduler_not_available(self):
        """Error response when RefreshScheduler is not available."""
        from src.code_indexer.server.web.routes import refresh_golden_repo

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
                refresh_golden_repo(
                    request=mock_request,
                    alias="my-repo",
                    csrf_token="valid-token",
                )
            finally:
                app_module.app.state = original_state

        call_kwargs = mock_page.call_args[1] if mock_page.call_args[1] else {}
        assert "error_message" in call_kwargs
        assert call_kwargs["error_message"] is not None
