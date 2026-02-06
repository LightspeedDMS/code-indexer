"""
Tests for Credential Diagnostics (Story S5 - AC3, AC4, AC5).

Tests credential diagnostic methods:
- AC3: GitLab Token diagnostic validates format AND tests API call
- AC4: Claude Delegation Credentials diagnostic tests authentication
- AC5: run_credential_diagnostics() runs all checks in parallel
- run_category() dispatches CREDENTIALS category
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import HTTPStatusError, Request, Response, TimeoutException

from code_indexer.server.services.diagnostics_service import (
    DiagnosticsService,
    DiagnosticStatus,
    DiagnosticCategory,
    API_TIMEOUT_SECONDS,
)


class TestCheckGitLabToken:
    """Tests for check_gitlab_token() method (AC3)."""

    @pytest.mark.asyncio
    async def test_gitlab_token_working(self):
        """Test GitLab token working with valid format and API call."""
        service = DiagnosticsService()

        # Mock CITokenManager returning valid GitLab token
        mock_token_data = MagicMock()
        mock_token_data.token = "glpat-" + "x" * 20
        mock_token_data.base_url = "https://gitlab.com"

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            # Mock httpx client for API call
            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()
                mock_response.json = MagicMock(return_value={"username": "testuser"})
                mock_client.get = AsyncMock(return_value=mock_response)

                result = await service.check_gitlab_token()

        assert result.name == "GitLab Token"
        assert result.status == DiagnosticStatus.WORKING
        assert "valid" in result.message.lower() or "working" in result.message.lower()
        assert result.details.get("username") == "testuser"

    @pytest.mark.asyncio
    async def test_gitlab_token_not_configured(self):
        """Test GitLab token not configured returns NOT_CONFIGURED."""
        service = DiagnosticsService()

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = None

            result = await service.check_gitlab_token()

        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_gitlab_token_invalid_format_warning(self):
        """Test GitLab token with invalid format returns WARNING."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "invalid_token_format"

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            result = await service.check_gitlab_token()

        assert result.status == DiagnosticStatus.WARNING
        assert "format" in result.message.lower()

    @pytest.mark.asyncio
    async def test_gitlab_token_api_call_fails_401(self):
        """Test GitLab token API call failing with 401 Unauthorized."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "glpat-" + "x" * 20
        mock_token_data.base_url = "https://gitlab.com"

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response_obj = Response(
                    status_code=401,
                    request=Request("GET", "https://gitlab.com/api/v4/user"),
                )
                mock_client.get = AsyncMock(
                    side_effect=HTTPStatusError(
                        "Unauthorized",
                        request=mock_response_obj.request,
                        response=mock_response_obj,
                    )
                )

                result = await service.check_gitlab_token()

        assert result.status == DiagnosticStatus.ERROR
        assert "401" in result.message or "unauthorized" in result.message.lower()

    @pytest.mark.asyncio
    async def test_gitlab_token_timeout(self):
        """Test GitLab token API call timing out after 30 seconds."""
        service = DiagnosticsService()

        mock_token_data = MagicMock()
        mock_token_data.token = "glpat-" + "x" * 20
        mock_token_data.base_url = "https://gitlab.com"

        with patch(
            "code_indexer.server.services.diagnostics_service.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_client.get = AsyncMock(side_effect=TimeoutException("Timeout"))

                result = await service.check_gitlab_token()

        assert result.status == DiagnosticStatus.ERROR
        assert "timeout" in result.message.lower() or "timed out" in result.message.lower()


class TestCheckClaudeDelegationCredentials:
    """Tests for check_claude_delegation_credentials() method (AC4)."""

    @pytest.mark.asyncio
    async def test_claude_delegation_working(self):
        """Test Claude delegation credentials working with successful auth."""
        service = DiagnosticsService()

        # Mock ClaudeDelegationManager returning valid config
        mock_config = MagicMock()
        mock_config.is_configured = True
        mock_config.claude_server_url = "https://claude.example.com"
        mock_config.claude_server_username = "admin"
        mock_config.claude_server_credential = "password123"

        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.load_config.return_value = mock_config

            # Mock httpx client for login API call
            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()
                mock_response.json = MagicMock(
                    return_value={"access_token": "jwt_token_here"}
                )
                mock_client.post = AsyncMock(return_value=mock_response)

                result = await service.check_claude_delegation_credentials()

        assert result.name == "Claude Delegation Credentials"
        assert result.status == DiagnosticStatus.WORKING
        assert "valid" in result.message.lower() or "working" in result.message.lower()

    @pytest.mark.asyncio
    async def test_claude_delegation_not_configured(self):
        """Test Claude delegation not configured returns NOT_CONFIGURED."""
        service = DiagnosticsService()

        # Mock ClaudeDelegationManager returning unconfigured config
        mock_config = MagicMock()
        mock_config.is_configured = False

        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.load_config.return_value = mock_config

            result = await service.check_claude_delegation_credentials()

        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_claude_delegation_auth_error_401(self):
        """Test Claude delegation credentials failing with 401 Unauthorized."""
        service = DiagnosticsService()

        mock_config = MagicMock()
        mock_config.is_configured = True
        mock_config.claude_server_url = "https://claude.example.com"
        mock_config.claude_server_username = "admin"
        mock_config.claude_server_credential = "wrong_password"

        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.load_config.return_value = mock_config

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_response_obj = Response(
                    status_code=401,
                    request=Request(
                        "POST", "https://claude.example.com/auth/login"
                    ),
                )
                mock_client.post = AsyncMock(
                    side_effect=HTTPStatusError(
                        "Unauthorized",
                        request=mock_response_obj.request,
                        response=mock_response_obj,
                    )
                )

                result = await service.check_claude_delegation_credentials()

        assert result.status == DiagnosticStatus.ERROR
        assert "401" in result.message or "authentication failed" in result.message.lower()

    @pytest.mark.asyncio
    async def test_claude_delegation_timeout(self):
        """Test Claude delegation credentials timing out."""
        service = DiagnosticsService()

        mock_config = MagicMock()
        mock_config.is_configured = True
        mock_config.claude_server_url = "https://claude.example.com"
        mock_config.claude_server_username = "admin"
        mock_config.claude_server_credential = "password123"

        with patch(
            "code_indexer.server.services.diagnostics_service.ClaudeDelegationManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.load_config.return_value = mock_config

            with patch(
                "code_indexer.server.services.diagnostics_service.httpx.AsyncClient"
            ) as mock_client_class:
                mock_client = mock_client_class.return_value.__aenter__.return_value
                mock_client.post = AsyncMock(side_effect=TimeoutException("Timeout"))

                result = await service.check_claude_delegation_credentials()

        assert result.status == DiagnosticStatus.ERROR
        assert "timeout" in result.message.lower() or "timed out" in result.message.lower()


class TestRunCredentialDiagnostics:
    """Tests for run_credential_diagnostics() method (AC5)."""

    @pytest.mark.asyncio
    async def test_run_credential_diagnostics_returns_all_checks(self):
        """Test run_credential_diagnostics() returns results from all 4 credential checks."""
        service = DiagnosticsService()

        # Mock all credential check methods
        mock_ssh_result = MagicMock()
        mock_ssh_result.name = "SSH Keys"
        mock_ssh_result.status = DiagnosticStatus.WORKING

        mock_github_result = MagicMock()
        mock_github_result.name = "GitHub Token"
        mock_github_result.status = DiagnosticStatus.WORKING

        mock_gitlab_result = MagicMock()
        mock_gitlab_result.name = "GitLab Token"
        mock_gitlab_result.status = DiagnosticStatus.NOT_CONFIGURED

        mock_claude_result = MagicMock()
        mock_claude_result.name = "Claude Delegation Credentials"
        mock_claude_result.status = DiagnosticStatus.WORKING

        with patch.object(
            service, "check_ssh_keys", return_value=mock_ssh_result
        ) as mock_ssh:
            with patch.object(
                service, "check_github_token", return_value=mock_github_result
            ) as mock_github:
                with patch.object(
                    service, "check_gitlab_token", return_value=mock_gitlab_result
                ) as mock_gitlab:
                    with patch.object(
                        service,
                        "check_claude_delegation_credentials",
                        return_value=mock_claude_result,
                    ) as mock_claude:
                        results = await service.run_credential_diagnostics()

        # Verify all checks were called
        mock_ssh.assert_called_once()
        mock_github.assert_called_once()
        mock_gitlab.assert_called_once()
        mock_claude.assert_called_once()

        # Verify all results returned
        assert len(results) == 4
        result_names = {r.name for r in results}
        assert "SSH Keys" in result_names
        assert "GitHub Token" in result_names
        assert "GitLab Token" in result_names
        assert "Claude Delegation Credentials" in result_names

    @pytest.mark.asyncio
    async def test_run_credential_diagnostics_parallel_execution(self):
        """Test run_credential_diagnostics() runs checks in parallel using asyncio.gather."""
        service = DiagnosticsService()

        # Track call order to verify parallelism
        call_order = []

        async def mock_ssh_keys():
            call_order.append("ssh")
            return MagicMock(name="SSH Keys", status=DiagnosticStatus.WORKING)

        async def mock_github_token():
            call_order.append("github")
            return MagicMock(name="GitHub Token", status=DiagnosticStatus.WORKING)

        async def mock_gitlab_token():
            call_order.append("gitlab")
            return MagicMock(name="GitLab Token", status=DiagnosticStatus.WORKING)

        async def mock_claude_delegation():
            call_order.append("claude")
            return MagicMock(
                name="Claude Delegation Credentials", status=DiagnosticStatus.WORKING
            )

        with patch.object(service, "check_ssh_keys", side_effect=mock_ssh_keys):
            with patch.object(
                service, "check_github_token", side_effect=mock_github_token
            ):
                with patch.object(
                    service, "check_gitlab_token", side_effect=mock_gitlab_token
                ):
                    with patch.object(
                        service,
                        "check_claude_delegation_credentials",
                        side_effect=mock_claude_delegation,
                    ):
                        results = await service.run_credential_diagnostics()

        # All 4 checks should have been executed
        assert len(call_order) == 4
        assert len(results) == 4


class TestRunCategoryCredentials:
    """Tests for run_category() dispatching CREDENTIALS category."""

    @pytest.mark.asyncio
    async def test_run_category_dispatches_credentials(self):
        """Test run_category() dispatches to run_credential_diagnostics() for CREDENTIALS category."""
        import tempfile
        import os

        # Use temporary database to avoid cache from DB
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            # Clear cache to ensure fresh run
            service.clear_cache(DiagnosticCategory.CREDENTIALS)

            # Mock run_credential_diagnostics()
            mock_results = [
                MagicMock(name="SSH Keys", status=DiagnosticStatus.WORKING),
                MagicMock(name="GitHub Token", status=DiagnosticStatus.WORKING),
                MagicMock(name="GitLab Token", status=DiagnosticStatus.NOT_CONFIGURED),
                MagicMock(
                    name="Claude Delegation Credentials", status=DiagnosticStatus.WORKING
                ),
            ]

            with patch.object(
                service, "run_credential_diagnostics", new=AsyncMock(return_value=mock_results)
            ) as mock_run_creds:
                await service.run_category(DiagnosticCategory.CREDENTIALS)

                # Verify run_credential_diagnostics was called
                mock_run_creds.assert_called_once()

            # Verify results were cached
            cached_results = service.get_category_status(DiagnosticCategory.CREDENTIALS)
            assert len(cached_results) == 4
            assert cached_results == mock_results
        finally:
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)


class TestTokenManagerSQLiteBackend:
    """Tests for Bug #146: Verify CITokenManager uses SQLite backend."""

    @pytest.mark.asyncio
    async def test_check_github_api_uses_sqlite_backend(self):
        """Test check_github_api() creates CITokenManager with SQLite backend."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            with patch(
                "code_indexer.server.services.diagnostics_service.CITokenManager"
            ) as mock_manager_class:
                # Mock to return None (not configured) to avoid API call
                mock_manager = mock_manager_class.return_value
                mock_manager.get_token.return_value = None

                await service.check_github_api()

                # Verify CITokenManager was created with SQLite backend
                mock_manager_class.assert_called_once()
                call_kwargs = mock_manager_class.call_args[1]
                assert call_kwargs.get("use_sqlite") is True
                assert call_kwargs.get("db_path") == tmp_db_path
        finally:
            import os
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)

    @pytest.mark.asyncio
    async def test_check_gitlab_api_uses_sqlite_backend(self):
        """Test check_gitlab_api() creates CITokenManager with SQLite backend."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            with patch(
                "code_indexer.server.services.diagnostics_service.CITokenManager"
            ) as mock_manager_class:
                # Mock to return None (not configured) to avoid API call
                mock_manager = mock_manager_class.return_value
                mock_manager.get_token.return_value = None

                await service.check_gitlab_api()

                # Verify CITokenManager was created with SQLite backend
                mock_manager_class.assert_called_once()
                call_kwargs = mock_manager_class.call_args[1]
                assert call_kwargs.get("use_sqlite") is True
                assert call_kwargs.get("db_path") == tmp_db_path
        finally:
            import os
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)

    @pytest.mark.asyncio
    async def test_check_github_token_uses_sqlite_backend(self):
        """Test check_github_token() creates CITokenManager with SQLite backend."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            with patch(
                "code_indexer.server.services.diagnostics_service.CITokenManager"
            ) as mock_manager_class:
                # Mock to return None (not configured) to avoid API call
                mock_manager = mock_manager_class.return_value
                mock_manager.get_token.return_value = None

                await service.check_github_token()

                # Verify CITokenManager was created with SQLite backend
                mock_manager_class.assert_called_once()
                call_kwargs = mock_manager_class.call_args[1]
                assert call_kwargs.get("use_sqlite") is True
                assert call_kwargs.get("db_path") == tmp_db_path
        finally:
            import os
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)

    @pytest.mark.asyncio
    async def test_check_gitlab_token_uses_sqlite_backend(self):
        """Test check_gitlab_token() creates CITokenManager with SQLite backend."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            with patch(
                "code_indexer.server.services.diagnostics_service.CITokenManager"
            ) as mock_manager_class:
                # Mock to return None (not configured) to avoid API call
                mock_manager = mock_manager_class.return_value
                mock_manager.get_token.return_value = None

                await service.check_gitlab_token()

                # Verify CITokenManager was created with SQLite backend
                mock_manager_class.assert_called_once()
                call_kwargs = mock_manager_class.call_args[1]
                assert call_kwargs.get("use_sqlite") is True
                assert call_kwargs.get("db_path") == tmp_db_path
        finally:
            import os
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)
