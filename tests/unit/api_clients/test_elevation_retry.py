"""Unit tests for TOTP step-up elevation retry logic (Story #980).

Tests cover:
- ElevationRequiredError exception class
- ElevationFailedError exception class
- AuthAPIClient.elevate() method
- with_elevation_retry() wrapper (AC1, AC2, AC3)
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test: ElevationRequiredError exception class
# ---------------------------------------------------------------------------


class TestElevationRequiredError:
    """Tests for ElevationRequiredError exception."""

    def test_elevation_required_error_is_importable(self) -> None:
        """ElevationRequiredError must be importable from api_clients.elevation."""
        from code_indexer.api_clients.elevation import ElevationRequiredError

        assert ElevationRequiredError is not None

    def test_elevation_required_error_has_error_code_attribute(self) -> None:
        """ElevationRequiredError must carry an error_code attribute."""
        from code_indexer.api_clients.elevation import ElevationRequiredError

        err = ElevationRequiredError(error_code="elevation_required")
        assert err.error_code == "elevation_required"

    def test_elevation_required_error_stores_setup_url(self) -> None:
        """ElevationRequiredError must optionally carry a setup_url."""
        from code_indexer.api_clients.elevation import ElevationRequiredError

        err = ElevationRequiredError(
            error_code="totp_setup_required",
            setup_url="/admin/mfa/setup",
        )
        assert err.setup_url == "/admin/mfa/setup"

    def test_elevation_required_error_default_setup_url_is_none(self) -> None:
        """setup_url defaults to None when not provided."""
        from code_indexer.api_clients.elevation import ElevationRequiredError

        err = ElevationRequiredError(error_code="elevation_required")
        assert err.setup_url is None

    def test_elevation_required_error_is_exception(self) -> None:
        """ElevationRequiredError must be an Exception subclass."""
        from code_indexer.api_clients.elevation import ElevationRequiredError

        err = ElevationRequiredError(error_code="elevation_required")
        assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# Test: ElevationFailedError exception class
# ---------------------------------------------------------------------------


class TestElevationFailedError:
    """Tests for ElevationFailedError exception."""

    def test_elevation_failed_error_is_importable(self) -> None:
        """ElevationFailedError must be importable from api_clients.elevation."""
        from code_indexer.api_clients.elevation import ElevationFailedError

        assert ElevationFailedError is not None

    def test_elevation_failed_error_is_exception(self) -> None:
        """ElevationFailedError must be an Exception subclass."""
        from code_indexer.api_clients.elevation import ElevationFailedError

        err = ElevationFailedError("Invalid TOTP code")
        assert isinstance(err, Exception)

    def test_elevation_failed_error_has_message(self) -> None:
        """ElevationFailedError must carry a message."""
        from code_indexer.api_clients.elevation import ElevationFailedError

        err = ElevationFailedError("Invalid TOTP code")
        assert "Invalid TOTP code" in str(err)


# ---------------------------------------------------------------------------
# Test: elevate() function
# ---------------------------------------------------------------------------


class TestElevateFunction:
    """Tests for the elevate() standalone function."""

    def test_elevate_is_importable(self) -> None:
        """elevate() must be importable from api_clients.elevation."""
        from code_indexer.api_clients.elevation import elevate

        assert callable(elevate)

    def test_elevate_calls_post_auth_elevate_endpoint(self) -> None:
        """elevate() must POST to /auth/elevate with totp_code."""
        from code_indexer.api_clients.elevation import elevate

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "elevated": True,
            "elevated_until": 1234567890.0,
            "scope": "full",
        }
        mock_session.post.return_value = mock_response

        elevate(
            session=mock_session,
            server_url="http://localhost:8000",
            token="test-token",
            totp_code="123456",
        )

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "/auth/elevate" in call_args[0][0]
        assert call_args[1]["json"]["totp_code"] == "123456"
        assert call_args[1]["headers"]["Authorization"] == "Bearer test-token"

    def test_elevate_returns_true_on_success(self) -> None:
        """elevate() returns True when server responds 200."""
        from code_indexer.api_clients.elevation import elevate

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "elevated": True,
            "elevated_until": 1234567890.0,
            "scope": "full",
        }
        mock_session.post.return_value = mock_response

        result = elevate(
            session=mock_session,
            server_url="http://localhost:8000",
            token="test-token",
            totp_code="123456",
        )

        assert result is True

    def test_elevate_raises_elevation_failed_on_401(self) -> None:
        """elevate() raises ElevationFailedError when server returns 401."""
        from code_indexer.api_clients.elevation import elevate, ElevationFailedError

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"error": "elevation_failed"}
        mock_session.post.return_value = mock_response

        with pytest.raises(ElevationFailedError):
            elevate(
                session=mock_session,
                server_url="http://localhost:8000",
                token="test-token",
                totp_code="wrongcode",
            )

    def test_elevate_raises_on_unexpected_status(self) -> None:
        """elevate() raises an exception when server returns unexpected status."""
        from code_indexer.api_clients.elevation import elevate

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"detail": "Internal Server Error"}
        mock_session.post.return_value = mock_response

        with pytest.raises(Exception):
            elevate(
                session=mock_session,
                server_url="http://localhost:8000",
                token="test-token",
                totp_code="123456",
            )


# ---------------------------------------------------------------------------
# Test: with_elevation_retry() wrapper (AC1, AC2, AC3)
# ---------------------------------------------------------------------------


class TestWithElevationRetry:
    """Tests for with_elevation_retry() wrapper function."""

    def test_with_elevation_retry_is_importable(self) -> None:
        """with_elevation_retry() must be importable from api_clients.elevation."""
        from code_indexer.api_clients.elevation import with_elevation_retry

        assert callable(with_elevation_retry)

    def test_with_elevation_retry_returns_result_on_success(self) -> None:
        """AC1: When no elevation required, returns API result unchanged."""
        from code_indexer.api_clients.elevation import with_elevation_retry

        expected_result = {"users": [{"username": "admin"}]}

        def api_call() -> Dict[str, Any]:
            return expected_result

        result = with_elevation_retry(
            fn=api_call,
            session=MagicMock(),
            server_url="http://localhost:8000",
            token="test-token",
            prompt_totp=lambda: "123456",
        )

        assert result == expected_result

    def test_with_elevation_retry_elevates_and_retries_on_elevation_required(
        self,
    ) -> None:
        """AC1: On elevation_required, prompts TOTP, elevates, and retries successfully."""
        from code_indexer.api_clients.elevation import (
            with_elevation_retry,
            ElevationRequiredError,
        )

        call_count = 0
        expected_result: Dict[str, Any] = {"users": []}

        def api_call() -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ElevationRequiredError(error_code="elevation_required")
            return expected_result

        mock_session = MagicMock()
        mock_elevate_response = MagicMock()
        mock_elevate_response.status_code = 200
        mock_elevate_response.json.return_value = {
            "elevated": True,
            "elevated_until": 9999999999.0,
            "scope": "full",
        }
        mock_session.post.return_value = mock_elevate_response

        result = with_elevation_retry(
            fn=api_call,
            session=mock_session,
            server_url="http://localhost:8000",
            token="test-token",
            prompt_totp=lambda: "123456",
        )

        assert result == expected_result
        assert call_count == 2  # called once, elevated, called again

    def test_with_elevation_retry_exits_on_wrong_totp(self) -> None:
        """AC2: On elevation_failed after wrong TOTP, raises SystemExit(1)."""
        from code_indexer.api_clients.elevation import (
            with_elevation_retry,
            ElevationRequiredError,
        )

        def api_call() -> Dict[str, Any]:
            raise ElevationRequiredError(error_code="elevation_required")

        mock_session = MagicMock()
        mock_elevate_response = MagicMock()
        mock_elevate_response.status_code = 401
        mock_elevate_response.json.return_value = {"error": "elevation_failed"}
        mock_session.post.return_value = mock_elevate_response

        with pytest.raises(SystemExit) as exc_info:
            with_elevation_retry(
                fn=api_call,
                session=mock_session,
                server_url="http://localhost:8000",
                token="test-token",
                prompt_totp=lambda: "wrongcode",
            )

        assert exc_info.value.code == 1

    def test_with_elevation_retry_exits_on_totp_setup_required(self) -> None:
        """AC3: On totp_setup_required, raises SystemExit(1) with setup message."""
        from code_indexer.api_clients.elevation import (
            with_elevation_retry,
            ElevationRequiredError,
        )

        def api_call() -> Dict[str, Any]:
            raise ElevationRequiredError(
                error_code="totp_setup_required",
                setup_url="/admin/mfa/setup",
            )

        with pytest.raises(SystemExit) as exc_info:
            with_elevation_retry(
                fn=api_call,
                session=MagicMock(),
                server_url="http://localhost:8000",
                token="test-token",
                prompt_totp=lambda: "123456",
            )

        assert exc_info.value.code == 1

    def test_with_elevation_retry_does_not_retry_twice(self) -> None:
        """with_elevation_retry makes only ONE retry after elevation (no infinite loop)."""
        from code_indexer.api_clients.elevation import (
            with_elevation_retry,
            ElevationRequiredError,
        )

        call_count = 0

        def api_call() -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise ElevationRequiredError(error_code="elevation_required")

        mock_session = MagicMock()
        mock_elevate_response = MagicMock()
        mock_elevate_response.status_code = 200
        mock_elevate_response.json.return_value = {
            "elevated": True,
            "elevated_until": 9999999999.0,
            "scope": "full",
        }
        mock_session.post.return_value = mock_elevate_response

        # After elevation, the second call still raises ElevationRequiredError
        # The wrapper should NOT loop indefinitely -- it should re-raise or exit
        with pytest.raises((ElevationRequiredError, SystemExit, Exception)):
            with_elevation_retry(
                fn=api_call,
                session=mock_session,
                server_url="http://localhost:8000",
                token="test-token",
                prompt_totp=lambda: "123456",
            )

        # Should have been called at most twice (original + one retry)
        assert call_count <= 2

    def test_with_elevation_retry_propagates_other_exceptions(self) -> None:
        """Non-elevation exceptions propagate unchanged from with_elevation_retry."""
        from code_indexer.api_clients.elevation import with_elevation_retry
        from code_indexer.api_clients.base_client import APIClientError

        def api_call() -> Dict[str, Any]:
            raise APIClientError("Some unrelated error", 500)

        with pytest.raises(APIClientError, match="Some unrelated error"):
            with_elevation_retry(
                fn=api_call,
                session=MagicMock(),
                server_url="http://localhost:8000",
                token="test-token",
                prompt_totp=lambda: "123456",
            )

    def test_with_elevation_retry_prints_totp_setup_message(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """AC3: totp_setup_required prints actionable error mentioning setup URL."""
        from code_indexer.api_clients.elevation import (
            with_elevation_retry,
            ElevationRequiredError,
        )

        def api_call() -> Dict[str, Any]:
            raise ElevationRequiredError(
                error_code="totp_setup_required",
                setup_url="/admin/mfa/setup",
            )

        with pytest.raises(SystemExit):
            with_elevation_retry(
                fn=api_call,
                session=MagicMock(),
                server_url="http://localhost:8000",
                token="test-token",
                prompt_totp=lambda: "123456",
            )

        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "setup" in output.lower() or "/admin/mfa/setup" in output

    def test_with_elevation_retry_prints_elevation_failed_message(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """AC2: elevation_failed prints a clear error message."""
        from code_indexer.api_clients.elevation import (
            with_elevation_retry,
            ElevationRequiredError,
        )

        def api_call() -> Dict[str, Any]:
            raise ElevationRequiredError(error_code="elevation_required")

        mock_session = MagicMock()
        mock_elevate_response = MagicMock()
        mock_elevate_response.status_code = 401
        mock_elevate_response.json.return_value = {"error": "elevation_failed"}
        mock_session.post.return_value = mock_elevate_response

        with pytest.raises(SystemExit):
            with_elevation_retry(
                fn=api_call,
                session=mock_session,
                server_url="http://localhost:8000",
                token="test-token",
                prompt_totp=lambda: "wrongcode",
            )

        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert (
            "elevation" in output.lower()
            or "totp" in output.lower()
            or "invalid" in output.lower()
        )


# ---------------------------------------------------------------------------
# Test: AdminAPIClient detects elevation_required in 403 response
# ---------------------------------------------------------------------------


class TestAdminClientElevationDetection:
    """AdminAPIClient must detect 403 elevation_required and raise ElevationRequiredError."""

    def _make_mock_response(self, status_code: int, json_data: Dict) -> MagicMock:
        """Build a mock httpx.Response."""
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    def test_list_users_raises_elevation_required_on_403_elevation_code(self) -> None:
        """AdminAPIClient.list_users() raises ElevationRequiredError on 403 elevation_required."""
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(
                403,
                {
                    "detail": {
                        "error": "elevation_required",
                        "message": "Elevation required",
                    }
                },
            ),
        ):
            with pytest.raises(ElevationRequiredError) as exc_info:
                client.list_users()

        assert exc_info.value.error_code == "elevation_required"

    def test_list_users_raises_elevation_required_on_403_totp_setup(self) -> None:
        """AdminAPIClient.list_users() raises ElevationRequiredError(totp_setup_required)."""
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(
                403,
                {
                    "detail": {
                        "error": "totp_setup_required",
                        "setup_url": "/admin/mfa/setup",
                        "message": "TOTP setup required",
                    }
                },
            ),
        ):
            with pytest.raises(ElevationRequiredError) as exc_info:
                client.list_users()

        assert exc_info.value.error_code == "totp_setup_required"
        assert exc_info.value.setup_url == "/admin/mfa/setup"

    def test_create_user_raises_elevation_required_on_403_elevation_code(self) -> None:
        """AdminAPIClient.create_user() raises ElevationRequiredError on 403 elevation_required."""
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(
                403,
                {
                    "detail": {
                        "error": "elevation_required",
                        "message": "Elevation required",
                    }
                },
            ),
        ):
            with pytest.raises(ElevationRequiredError):
                client.create_user(
                    username="newuser", password="Pass123!", role="normal_user"
                )

    def test_403_without_elevation_code_still_raises_authentication_error(self) -> None:
        """Regular 403 (e.g. insufficient privileges) still raises AuthenticationError."""
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.base_client import AuthenticationError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "normaluser", "password": "Pass123!"},
        )

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(
                403,
                {"detail": "Insufficient privileges"},
            ),
        ):
            with pytest.raises((AuthenticationError,)):
                client.list_users()

            # Must NOT raise ElevationRequiredError for plain 403


# ---------------------------------------------------------------------------
# Test: FastAPI error response unwrapping (Critical Fix #1)
# ---------------------------------------------------------------------------


class TestAdminClientFastAPIWrapping:
    """AdminAPIClient must unwrap FastAPI's {"detail": {"error": "..."}} format."""

    def _make_mock_response(self, status_code: int, json_data: Dict) -> MagicMock:
        """Build a mock httpx.Response."""
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    def test_list_users_detects_elevation_required_in_fastapi_detail_wrapper(
        self,
    ) -> None:
        """AdminAPIClient.list_users() detects elevation_required inside detail wrapper.

        FastAPI wraps HTTPException detail as {"detail": {"error": "elevation_required"}}.
        The production code must unwrap this, not read body.get("error") directly.
        """
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        # This is the ACTUAL format FastAPI sends over the wire
        fastapi_403_body = {"detail": {"error": "elevation_required"}}

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(403, fastapi_403_body),
        ):
            with pytest.raises(ElevationRequiredError) as exc_info:
                client.list_users()

        assert exc_info.value.error_code == "elevation_required"

    def test_list_users_detects_totp_setup_required_with_setup_url_in_detail(
        self,
    ) -> None:
        """AdminAPIClient.list_users() detects totp_setup_required and extracts setup_url.

        FastAPI wraps as {"detail": {"error": "totp_setup_required", "setup_url": "..."}}.
        The setup_url must be extracted from inside detail, not from the top-level body.
        """
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        fastapi_403_body = {
            "detail": {
                "error": "totp_setup_required",
                "setup_url": "/admin/mfa/setup",
            }
        }

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(403, fastapi_403_body),
        ):
            with pytest.raises(ElevationRequiredError) as exc_info:
                client.list_users()

        assert exc_info.value.error_code == "totp_setup_required"
        assert exc_info.value.setup_url == "/admin/mfa/setup"

    def test_create_user_detects_elevation_required_in_fastapi_detail_wrapper(
        self,
    ) -> None:
        """AdminAPIClient.create_user() detects elevation_required inside detail wrapper."""
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        fastapi_403_body = {"detail": {"error": "elevation_required"}}

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(403, fastapi_403_body),
        ):
            with pytest.raises(ElevationRequiredError):
                client.create_user(
                    username="newuser", password="Pass123!", role="normal_user"
                )

    def test_plain_string_detail_does_not_raise_elevation_error(self) -> None:
        """Plain string detail (e.g. insufficient privileges) must not trigger elevation."""
        from code_indexer.api_clients.admin_client import AdminAPIClient
        from code_indexer.api_clients.base_client import AuthenticationError

        client = AdminAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "normaluser", "password": "Pass123!"},
        )

        # FastAPI with plain HTTPException("Insufficient privileges") -> {"detail": "..."}
        fastapi_403_plain = {"detail": "Insufficient privileges"}

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mock_response(403, fastapi_403_plain),
        ):
            # AuthenticationError must be raised, NOT ElevationRequiredError
            with pytest.raises(AuthenticationError):
                client.list_users()


# ---------------------------------------------------------------------------
# Test: GroupAPIClient MCP elevation detection (Critical Fix #2)
# ---------------------------------------------------------------------------


class TestGroupClientElevationDetection:
    """GroupAPIClient must detect elevation error codes in MCP tool responses."""

    def _make_mcp_content_response(self, content_dict: Dict) -> MagicMock:
        """Build a mock httpx.Response for MCP JSON-RPC with content in result."""
        import json as json_module

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json_module.dumps(content_dict)}]
            },
        }
        return mock_resp

    def test_list_groups_raises_elevation_required_when_mcp_returns_elevation_code(
        self,
    ) -> None:
        """GroupAPIClient.list_groups() raises ElevationRequiredError on MCP elevation_required.

        The MCP elevation decorator returns {"error": "elevation_required"} inside
        the content text when enforcement is enabled and no elevation window exists.
        """
        from code_indexer.api_clients.group_client import GroupAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = GroupAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        mcp_elevation_response = {
            "error": "elevation_required",
            "message": "No elevation window",
        }

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mcp_content_response(mcp_elevation_response),
        ):
            with pytest.raises(ElevationRequiredError) as exc_info:
                client.list_groups()

        assert exc_info.value.error_code == "elevation_required"

    def test_create_group_raises_elevation_required_when_mcp_returns_elevation_code(
        self,
    ) -> None:
        """GroupAPIClient.create_group() raises ElevationRequiredError on MCP elevation_required."""
        from code_indexer.api_clients.group_client import GroupAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = GroupAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        mcp_elevation_response = {
            "error": "elevation_required",
            "message": "No elevation window",
        }

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mcp_content_response(mcp_elevation_response),
        ):
            with pytest.raises(ElevationRequiredError):
                client.create_group(name="testgroup")

    def test_list_groups_raises_elevation_required_on_totp_setup_required(
        self,
    ) -> None:
        """GroupAPIClient.list_groups() raises ElevationRequiredError on totp_setup_required."""
        from code_indexer.api_clients.group_client import GroupAPIClient
        from code_indexer.api_clients.elevation import ElevationRequiredError

        client = GroupAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        mcp_totp_setup_response = {
            "error": "totp_setup_required",
            "setup_url": "/admin/mfa/setup",
            "message": "Set up TOTP",
        }

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mcp_content_response(mcp_totp_setup_response),
        ):
            with pytest.raises(ElevationRequiredError) as exc_info:
                client.list_groups()

        assert exc_info.value.error_code == "totp_setup_required"
        assert exc_info.value.setup_url == "/admin/mfa/setup"

    def test_regular_mcp_error_still_raises_api_client_error(self) -> None:
        """Non-elevation MCP errors raise APIClientError, not ElevationRequiredError."""
        from code_indexer.api_clients.group_client import GroupAPIClient
        from code_indexer.api_clients.base_client import APIClientError

        client = GroupAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "admin"},
        )

        mcp_regular_error = {"success": False, "error": "Group manager not initialized"}

        with patch.object(
            client,
            "_authenticated_request",
            return_value=self._make_mcp_content_response(mcp_regular_error),
        ):
            with pytest.raises(APIClientError):
                client.list_groups()
