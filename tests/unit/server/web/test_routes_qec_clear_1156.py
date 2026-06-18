"""Story #1156: clear_query_embedding_cache_table endpoint tests.

Tests for POST /config/query-embedding-cache/clear route.

Covers:
- AC4: admin-only — 401 returned when no valid session
- AC4: CSRF validation — 403 returned on missing/invalid token
- AC4/AC5: successful clear returns 200 HTML with count 0
- AC7: clear when no cache wired returns success (no-op)
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch


def _make_mock_request(csrf_header: Optional[str] = "valid-csrf-token") -> MagicMock:
    """Return a mock FastAPI Request with X-CSRF-Token header."""
    request = MagicMock()
    request.headers.get = lambda key, default=None: (
        csrf_header if key == "X-CSRF-Token" else default
    )
    return request


class TestClearQueryEmbeddingCacheEndpoint:
    def test_returns_401_when_no_admin_session(self) -> None:
        """Unauthenticated request gets 401 (AC4 admin-only)."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        request = _make_mock_request()

        with patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=None,
        ):
            response = clear_query_embedding_cache_table(request)

        assert response.status_code == 401

    def test_returns_403_when_csrf_invalid(self) -> None:
        """Bad CSRF token gets 403 (AC4 CSRF validation)."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        session = MagicMock()
        request = _make_mock_request(csrf_header="bad-token")

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=False,
            ),
        ):
            response = clear_query_embedding_cache_table(request)

        assert response.status_code == 403

    def test_returns_403_when_no_csrf_header(self) -> None:
        """Missing CSRF header also gets 403."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        session = MagicMock()
        request = _make_mock_request(csrf_header=None)

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=False,
            ),
        ):
            response = clear_query_embedding_cache_table(request)

        assert response.status_code == 403

    def test_successful_clear_calls_clear_all(self) -> None:
        """Successful request calls cache.clear_all() (AC3/AC4)."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        session = MagicMock()
        request = _make_mock_request()
        mock_cache = MagicMock()
        mock_cache.clear_all = MagicMock()

        mock_template_response = MagicMock()
        mock_template_response.status_code = 200

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ),
            patch(
                "code_indexer.server.web.routes._get_qec_total_entries",
                return_value=0,
            ),
            patch(
                "code_indexer.server.web.routes._get_current_config",
                return_value={"query_embedding_cache": {"total_cached_entries": 5}},
            ),
            patch(
                "code_indexer.server.web.routes.generate_csrf_token",
                return_value="new-csrf",
            ),
            patch("code_indexer.server.web.routes.templates") as mock_templates,
        ):
            mock_templates.TemplateResponse.return_value = mock_template_response

            response = clear_query_embedding_cache_table(request)

        mock_cache.clear_all.assert_called_once()
        assert response.status_code == 200

    def test_successful_clear_injects_zero_count_into_config(self) -> None:
        """After clear, config dict has total_cached_entries = 0 (AC5)."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        session = MagicMock()
        request = _make_mock_request()
        mock_cache = MagicMock()

        captured_context: dict = {}

        def capture_template_response(template_name: str, context: dict):
            captured_context.update(context)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ),
            patch(
                "code_indexer.server.web.routes._get_qec_total_entries",
                return_value=0,
            ),
            patch(
                "code_indexer.server.web.routes._get_current_config",
                return_value={"query_embedding_cache": {"total_cached_entries": 999}},
            ),
            patch(
                "code_indexer.server.web.routes.generate_csrf_token",
                return_value="new-csrf",
            ),
            patch("code_indexer.server.web.routes.templates") as mock_templates,
        ):
            mock_templates.TemplateResponse.side_effect = capture_template_response

            clear_query_embedding_cache_table(request)

        # The config injected into the template must show 0, not the stale 999.
        assert (
            captured_context["config"]["query_embedding_cache"]["total_cached_entries"]
            == 0
        )

    def test_successful_clear_uses_qec_display_template(self) -> None:
        """The partial template returned is qec_display.html (AC5 HTMX swap)."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        session = MagicMock()
        request = _make_mock_request()
        mock_cache = MagicMock()

        captured_template_name: list = []

        def capture_template_response(template_name: str, context: dict):
            captured_template_name.append(template_name)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ),
            patch(
                "code_indexer.server.web.routes._get_qec_total_entries",
                return_value=0,
            ),
            patch(
                "code_indexer.server.web.routes._get_current_config",
                return_value={"query_embedding_cache": {"total_cached_entries": 0}},
            ),
            patch(
                "code_indexer.server.web.routes.generate_csrf_token",
                return_value="new-csrf",
            ),
            patch("code_indexer.server.web.routes.templates") as mock_templates,
        ):
            mock_templates.TemplateResponse.side_effect = capture_template_response

            clear_query_embedding_cache_table(request)

        assert captured_template_name[0] == "partials/qec_display.html"

    def test_none_cache_is_noop_success(self) -> None:
        """When no cache is wired (CLI/pre-lifespan), returns 200 with count 0 (AC7)."""
        from code_indexer.server.web.routes import clear_query_embedding_cache_table

        session = MagicMock()
        request = _make_mock_request()

        mock_template_response = MagicMock()
        mock_template_response.status_code = 200

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=None,
            ),
            patch(
                "code_indexer.server.web.routes._get_qec_total_entries",
                return_value=0,
            ),
            patch(
                "code_indexer.server.web.routes._get_current_config",
                return_value={"query_embedding_cache": {"total_cached_entries": 0}},
            ),
            patch(
                "code_indexer.server.web.routes.generate_csrf_token",
                return_value="new-csrf",
            ),
            patch("code_indexer.server.web.routes.templates") as mock_templates,
        ):
            mock_templates.TemplateResponse.return_value = mock_template_response

            response = clear_query_embedding_cache_table(request)

        assert response.status_code == 200
