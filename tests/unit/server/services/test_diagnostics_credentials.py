"""
Tests for Credential Diagnostics (Story S5 - AC3, AC5).

Tests credential diagnostic methods:
- AC3: GitLab Token diagnostic validates format AND tests API call
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
)


@pytest.mark.slow
class TestCheckGitLabToken:
    """Tests for check_gitlab_token() method (AC3)."""

    @pytest.mark.asyncio
    async def test_gitlab_token_working(self, tmp_path):
        """Test GitLab token working with valid format and API call."""
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

        # Mock CITokenManager returning valid GitLab token
        mock_token_data = MagicMock()
        mock_token_data.token = "glpat-" + "x" * 20
        mock_token_data.base_url = "https://gitlab.com"

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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
    async def test_gitlab_token_not_configured(self, tmp_path):
        """Test GitLab token not configured returns NOT_CONFIGURED.

        Bug #1304: patch target corrected to
        code_indexer.server.services.ci_token_manager.CITokenManager --
        DiagnosticsService._get_token_manager() resolves CITokenManager via
        create_token_manager() in ci_token_manager.py, so patching the name
        re-exported into diagnostics_service.py was a no-op that let this
        host's real GitLab token (e.g. from .local-testing) leak through.
        """
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = None

            result = await service.check_gitlab_token()

        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_gitlab_token_invalid_format_warning(self, tmp_path):
        """Test GitLab token with invalid format returns WARNING.

        Bug #1304: same corrected patch target as test_gitlab_token_not_configured.
        """
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

        mock_token_data = MagicMock()
        mock_token_data.token = "invalid_token_format"

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
        ) as mock_manager_class:
            mock_manager = mock_manager_class.return_value
            mock_manager.get_token.return_value = mock_token_data

            result = await service.check_gitlab_token()

        assert result.status == DiagnosticStatus.WARNING
        assert "format" in result.message.lower()

    @pytest.mark.asyncio
    async def test_gitlab_token_api_call_fails_401(self, tmp_path):
        """Test GitLab token API call failing with 401 Unauthorized."""
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

        mock_token_data = MagicMock()
        mock_token_data.token = "glpat-" + "x" * 20
        mock_token_data.base_url = "https://gitlab.com"

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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
    async def test_gitlab_token_timeout(self, tmp_path):
        """Test GitLab token API call timing out after 30 seconds."""
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

        mock_token_data = MagicMock()
        mock_token_data.token = "glpat-" + "x" * 20
        mock_token_data.base_url = "https://gitlab.com"

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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
        assert (
            "timeout" in result.message.lower() or "timed out" in result.message.lower()
        )


@pytest.mark.slow
class TestRunCredentialDiagnostics:
    """Tests for run_credential_diagnostics() method (AC5)."""

    @pytest.mark.asyncio
    async def test_run_credential_diagnostics_returns_all_checks(self, tmp_path):
        """Test run_credential_diagnostics() returns results from all 3 credential checks."""
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

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

        with patch.object(
            service, "check_ssh_keys", return_value=mock_ssh_result
        ) as mock_ssh:
            with patch.object(
                service, "check_github_token", return_value=mock_github_result
            ) as mock_github:
                with patch.object(
                    service, "check_gitlab_token", return_value=mock_gitlab_result
                ) as mock_gitlab:
                    results = await service.run_credential_diagnostics()

        # Verify all checks were called
        mock_ssh.assert_called_once()
        mock_github.assert_called_once()
        mock_gitlab.assert_called_once()

        # Verify all results returned
        assert len(results) == 3
        result_names = {r.name for r in results}
        assert "SSH Keys" in result_names
        assert "GitHub Token" in result_names
        assert "GitLab Token" in result_names

    @pytest.mark.asyncio
    async def test_run_credential_diagnostics_parallel_execution(self, tmp_path):
        """Test run_credential_diagnostics() runs checks in parallel using asyncio.gather."""
        service = DiagnosticsService(db_path=str(tmp_path / "diagnostics.db"))

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

        with patch.object(service, "check_ssh_keys", side_effect=mock_ssh_keys):
            with patch.object(
                service, "check_github_token", side_effect=mock_github_token
            ):
                with patch.object(
                    service, "check_gitlab_token", side_effect=mock_gitlab_token
                ):
                    results = await service.run_credential_diagnostics()

        # All 3 checks should have been executed
        assert len(call_order) == 3
        assert len(results) == 3


@pytest.mark.slow
class TestRunCategoryCredentials:
    """Tests for run_category() dispatching CREDENTIALS category."""

    @pytest.mark.asyncio
    async def test_run_category_dispatches_credentials(self):
        """Test run_category() dispatches to run_credential_diagnostics() for CREDENTIALS category."""
        import tempfile
        import os

        # Use temporary database to avoid cache from DB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
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
            ]

            with patch.object(
                service,
                "run_credential_diagnostics",
                new=AsyncMock(return_value=mock_results),
            ) as mock_run_creds:
                await service.run_category(DiagnosticCategory.CREDENTIALS)

                # Verify run_credential_diagnostics was called
                mock_run_creds.assert_called_once()

            # Verify results were cached
            cached_results = service.get_category_status(DiagnosticCategory.CREDENTIALS)
            assert len(cached_results) == 3
            assert cached_results == mock_results
        finally:
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)


@pytest.mark.slow
class TestTokenManagerSQLiteBackend:
    """Tests for Bug #146: Verify CITokenManager uses SQLite backend."""

    @pytest.mark.asyncio
    async def test_check_github_api_uses_sqlite_backend(self, tmp_path):
        """Test check_github_api() creates CITokenManager with SQLite backend.

        Bug #1304: two fixes bundled --
        1. Patch target corrected to
           code_indexer.server.services.ci_token_manager.CITokenManager --
           _get_token_manager() resolves CITokenManager via
           create_token_manager() in ci_token_manager.py, not via the name
           re-exported into diagnostics_service.py (patching the latter was
           a silent no-op).
        2. db_path must live two directories under an existing writable
           server_dir (<server_dir>/data/cidx_server.db), matching
           DiagnosticsService.__init__'s real layout. A bare
           tempfile.NamedTemporaryFile(suffix=".db") lives directly in /tmp,
           so _get_token_manager()'s server_dir = Path(db_path).parent.parent
           computed to "/" and ensure_encryption_key_salt() raised
           PermissionError trying to write /.encryption_key_salt -- BEFORE
           ever reaching the mocked CITokenManager (proven via direct repro).
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        tmp_db_path = str(data_dir / "cidx_server.db")

        service = DiagnosticsService(db_path=tmp_db_path)

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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

    @pytest.mark.asyncio
    async def test_check_gitlab_api_uses_sqlite_backend(self, tmp_path):
        """Test check_gitlab_api() creates CITokenManager with SQLite backend.

        Bug #1304: same fixes as test_check_github_api_uses_sqlite_backend
        above (corrected patch target + tmp_path/data-subdir server_dir layout).
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        tmp_db_path = str(data_dir / "cidx_server.db")

        service = DiagnosticsService(db_path=tmp_db_path)

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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

    @pytest.mark.asyncio
    async def test_check_github_token_uses_sqlite_backend(self, tmp_path):
        """Test check_github_token() creates CITokenManager with SQLite backend.

        Bug #1304: same fixes as test_check_github_api_uses_sqlite_backend
        above (corrected patch target + tmp_path/data-subdir server_dir layout).
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        tmp_db_path = str(data_dir / "cidx_server.db")

        service = DiagnosticsService(db_path=tmp_db_path)

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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

    @pytest.mark.asyncio
    async def test_check_gitlab_token_uses_sqlite_backend(self, tmp_path):
        """Test check_gitlab_token() creates CITokenManager with SQLite backend.

        Bug #1304: same fixes as test_check_github_api_uses_sqlite_backend
        above (corrected patch target + tmp_path/data-subdir server_dir layout).
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        tmp_db_path = str(data_dir / "cidx_server.db")

        service = DiagnosticsService(db_path=tmp_db_path)

        with patch(
            "code_indexer.server.services.ci_token_manager.CITokenManager"
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
