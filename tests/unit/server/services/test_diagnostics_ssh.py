"""
Tests for SSH Keys Diagnostics (Story S5 - AC1, AC6, AC8).

Tests SSH key connectivity diagnostic:
- AC1: SSH Keys diagnostic runs `ssh -T git@github.com`, returns WORKING/ERROR/NOT_CONFIGURED
- AC6: SSH check has 60-second timeout
- AC8: Shows "Not Configured" status when SSH not available
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from code_indexer.server.services.diagnostics_service import (
    DiagnosticsService,
    DiagnosticStatus,
    SSH_TIMEOUT_SECONDS,
)


class TestCheckSSHKeys:
    """Tests for check_ssh_keys() method (AC1, AC6, AC8)."""

    @pytest.mark.asyncio
    async def test_ssh_keys_working_github(self):
        """Test SSH keys working with GitHub authentication."""
        service = DiagnosticsService()

        # Mock subprocess that returns successful GitHub SSH auth
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(
                b"",
                b"Hi username! You've successfully authenticated, but GitHub does not provide shell access.",
            )
        )
        mock_process.returncode = 1  # GitHub returns 1 on successful auth

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_process
        ) as mock_exec:
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
                # wait_for should return the process
                mock_wait_for.return_value = mock_process

                result = await service.check_ssh_keys()

        assert result.name == "SSH Keys"
        assert result.status == DiagnosticStatus.WORKING
        assert "authentication successful" in result.message.lower()
        assert "output" in result.details

        # Verify SSH command structure
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args[0][0] == "ssh"
        assert "-T" in call_args[0]
        assert "git@github.com" in call_args[0]

    @pytest.mark.asyncio
    async def test_ssh_keys_working_gitlab(self):
        """Test SSH keys working with GitLab authentication."""
        service = DiagnosticsService()

        # Mock subprocess that returns successful GitLab SSH auth
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(
                b"",
                b"Welcome to GitLab, @username!",
            )
        )
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
                mock_wait_for.return_value = mock_process

                result = await service.check_ssh_keys()

        assert result.status == DiagnosticStatus.WORKING
        assert "authentication successful" in result.message.lower()

    @pytest.mark.asyncio
    async def test_ssh_keys_permission_denied(self):
        """Test SSH keys returning permission denied (no key or wrong key)."""
        service = DiagnosticsService()

        # Mock subprocess that returns permission denied
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(
                b"",
                b"git@github.com: Permission denied (publickey).",
            )
        )
        mock_process.returncode = 255

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
                mock_wait_for.return_value = mock_process

                result = await service.check_ssh_keys()

        assert result.status == DiagnosticStatus.ERROR
        assert "permission denied" in result.message.lower()
        assert result.details.get("exit_code") == 255

    @pytest.mark.asyncio
    async def test_ssh_keys_timeout(self):
        """Test SSH check timing out after 60 seconds (AC6)."""
        service = DiagnosticsService()

        # Mock timeout from wait_for
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            result = await service.check_ssh_keys()

        assert result.status == DiagnosticStatus.ERROR
        assert "timed out" in result.message.lower()
        assert result.details.get("timeout_seconds") == SSH_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_ssh_keys_not_configured_ssh_not_installed(self):
        """Test SSH not installed (FileNotFoundError) returns NOT_CONFIGURED (AC8)."""
        service = DiagnosticsService()

        # Mock FileNotFoundError when trying to run ssh command
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            with patch("asyncio.wait_for", side_effect=FileNotFoundError):
                result = await service.check_ssh_keys()

        assert result.status == DiagnosticStatus.NOT_CONFIGURED
        assert "not installed" in result.message.lower() or "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_ssh_keys_host_key_verification_failed(self):
        """Test SSH host key verification failure returns ERROR."""
        service = DiagnosticsService()

        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(
                b"",
                b"Host key verification failed.",
            )
        )
        mock_process.returncode = 255

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
                mock_wait_for.return_value = mock_process

                result = await service.check_ssh_keys()

        assert result.status == DiagnosticStatus.ERROR
        assert "verification" in result.message.lower() or "error" in result.message.lower()


class TestSSHTimeoutConstant:
    """Tests for SSH_TIMEOUT_SECONDS constant (AC6)."""

    def test_ssh_timeout_constant_exists(self):
        """Test SSH_TIMEOUT_SECONDS constant exists and is 60."""
        assert SSH_TIMEOUT_SECONDS == 60.0
