"""
Unit tests for CLI Tools and SDK Prerequisites Diagnostics (Story #91).

Tests cover:
- CLI tool availability checks
- SDK prerequisite checks
- Version extraction from command output
- Timeout behavior (10 seconds)
- Parallel execution with asyncio.gather
- SDK dependency mapping (SCIP tools)
- NOT_APPLICABLE status when SDK missing
- Error handling scenarios
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticStatus,
    DiagnosticsService,
)


class TestCheckCliTool:
    """Test check_cli_tool method for individual tool checks."""

    @pytest.mark.asyncio
    async def test_check_cli_tool_successful_command(self):
        """Test check_cli_tool with tool installed and working."""
        service = DiagnosticsService()

        # Mock subprocess to return successful version output
        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.stdout = b"ripgrep 13.0.0\n"
        mock_process.stderr = b""

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            mock_process.communicate = AsyncMock(return_value=(b"ripgrep 13.0.0\n", b""))

            result = await service.check_cli_tool("ripgrep", "rg --version")

            assert result.status == DiagnosticStatus.WORKING
            assert "ripgrep" in result.name
            assert "version" in result.details
            assert result.details["version"] == "13.0.0"
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_cli_tool_missing_tool(self):
        """Test check_cli_tool with tool not found (FileNotFoundError)."""
        service = DiagnosticsService()

        # Mock subprocess to raise FileNotFoundError (tool not in PATH)
        with patch(
            "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("rg not found")
        ):
            result = await service.check_cli_tool("ripgrep", "rg --version")

            assert result.status == DiagnosticStatus.NOT_CONFIGURED
            assert "not found" in result.message.lower() or "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_cli_tool_timeout(self):
        """Test check_cli_tool with command timeout (10 seconds)."""
        service = DiagnosticsService()

        # Mock subprocess that never completes
        async def never_completes():
            await asyncio.sleep(100)  # Much longer than timeout
            return (b"", b"")

        mock_process = Mock()
        mock_process.communicate = never_completes

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await service.check_cli_tool("slow-tool", "slow-tool --version")

            # Should timeout after 10 seconds and return ERROR status
            assert result.status == DiagnosticStatus.ERROR
            assert "timed out" in result.message.lower() or "timeout" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_cli_tool_non_zero_exit_code(self):
        """Test check_cli_tool with non-zero exit code."""
        service = DiagnosticsService()

        mock_process = Mock()
        mock_process.returncode = 1
        mock_process.stdout = b""
        mock_process.stderr = b"Error: invalid option\n"

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            mock_process.communicate = AsyncMock(return_value=(b"", b"Error: invalid option\n"))

            result = await service.check_cli_tool("broken-tool", "broken-tool --version")

            assert result.status == DiagnosticStatus.ERROR
            assert "exit code" in result.message.lower() or "error" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_cli_tool_not_applicable_when_sdk_missing(self):
        """Test check_cli_tool returns NOT_APPLICABLE when required SDK missing."""
        service = DiagnosticsService()

        # SDK availability dict shows nodejs is NOT available
        sdk_available = {"nodejs": False, "dotnet": True, "go": True}

        result = await service.check_cli_tool(
            "scip-typescript",
            "scip-typescript --version",
            required_sdk="nodejs",
            sdk_available=sdk_available,
        )

        assert result.status == DiagnosticStatus.NOT_APPLICABLE
        assert "sdk" in result.message.lower() or "nodejs" in result.message.lower()

    @pytest.mark.asyncio
    async def test_check_cli_tool_version_extraction_semver(self):
        """Test version extraction from semver format."""
        service = DiagnosticsService()

        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.stdout = b"scip-python version 0.3.1\n"
        mock_process.stderr = b""

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            mock_process.communicate = AsyncMock(return_value=(b"scip-python version 0.3.1\n", b""))

            result = await service.check_cli_tool("scip-python", "scip-python --version")

            assert result.status == DiagnosticStatus.WORKING
            assert "version" in result.details
            assert result.details["version"] == "0.3.1"

    @pytest.mark.asyncio
    async def test_check_cli_tool_version_extraction_date_based(self):
        """Test version extraction from date-based versions (git --version)."""
        service = DiagnosticsService()

        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.stdout = b"git version 2.34.1\n"
        mock_process.stderr = b""

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            mock_process.communicate = AsyncMock(return_value=(b"git version 2.34.1\n", b""))

            result = await service.check_cli_tool("Git", "git --version")

            assert result.status == DiagnosticStatus.WORKING
            assert "version" in result.details
            assert result.details["version"] == "2.34.1"


class TestRunCliToolDiagnostics:
    """Test run_cli_tool_diagnostics method."""

    @pytest.mark.asyncio
    async def test_run_cli_tool_diagnostics_returns_all_tools(self):
        """Test run_cli_tool_diagnostics returns results for all 8 CLI tools."""
        service = DiagnosticsService()

        # Mock successful version checks for all tools
        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.stdout = b"version 1.0.0\n"
        mock_process.stderr = b""

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            mock_process.communicate = AsyncMock(return_value=(b"version 1.0.0\n", b""))

            results = await service.run_cli_tool_diagnostics()

            # Should return exactly 8 results
            assert len(results) == 8

            # Check all expected tool names are present
            tool_names = [r.name for r in results]
            assert "ripgrep" in tool_names
            assert "Git" in tool_names
            assert "Coursier" in tool_names
            assert "Claude CLI" in tool_names
            assert "scip-python" in tool_names
            assert "scip-typescript" in tool_names
            assert "scip-dotnet" in tool_names
            assert "scip-go" in tool_names

    @pytest.mark.asyncio
    async def test_run_cli_tool_diagnostics_parallel_execution(self):
        """Test that run_cli_tool_diagnostics executes checks in parallel."""
        service = DiagnosticsService()

        # Mock each check to take 0.2 seconds
        async def slow_communicate():
            await asyncio.sleep(0.2)
            return (b"version 1.0.0\n", b"")

        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            start_time = asyncio.get_event_loop().time()
            results = await service.run_cli_tool_diagnostics()
            elapsed_time = asyncio.get_event_loop().time() - start_time

            # If parallel, should take ~0.2s for SDK checks + ~0.2s for CLI tools (not sequential)
            # Note: Implementation runs SDK checks first, then CLI tools in parallel
            # Allow overhead for SDK prerequisite checking and test execution
            assert elapsed_time < 1.0, f"Expected parallel execution <1.0s, got {elapsed_time}s"
            assert len(results) == 8

    @pytest.mark.asyncio
    async def test_run_cli_tool_diagnostics_sdk_dependency_mapping(self):
        """Test SDK dependency mapping - SCIP tools marked NOT_APPLICABLE when SDK missing."""
        service = DiagnosticsService()

        # Mock subprocess: dotnet missing (FileNotFoundError), others work
        async def mock_exec_factory(program, *args, **kwargs):
            mock_process = Mock()
            mock_process.returncode = 0

            if program == "dotnet":
                raise FileNotFoundError("dotnet not found")
            elif program == "scip-dotnet":
                # scip-dotnet should not be called if dotnet missing
                # But if called, return error
                mock_process.returncode = 1
                mock_process.communicate = AsyncMock(return_value=(b"", b"SDK missing"))
                return mock_process
            else:
                mock_process.communicate = AsyncMock(return_value=(b"version 1.0.0\n", b""))
                return mock_process

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec_factory):
            results = await service.run_cli_tool_diagnostics()

            # Find scip-dotnet result
            scip_dotnet_result = next((r for r in results if "scip-dotnet" in r.name), None)
            assert scip_dotnet_result is not None
            assert scip_dotnet_result.status == DiagnosticStatus.NOT_APPLICABLE


class TestRunSdkDiagnostics:
    """Test run_sdk_diagnostics method."""

    @pytest.mark.asyncio
    async def test_run_sdk_diagnostics_returns_all_sdks(self):
        """Test run_sdk_diagnostics returns results for all 3 SDKs."""
        service = DiagnosticsService()

        # Mock successful SDK checks
        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.stdout = b"1.0.0\n"
        mock_process.stderr = b""

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            mock_process.communicate = AsyncMock(return_value=(b"1.0.0\n", b""))

            results = await service.run_sdk_diagnostics()

            # Should return exactly 3 results
            assert len(results) == 3

            # Check all expected SDK names are present
            sdk_names = [r.name for r in results]
            assert ".NET SDK" in sdk_names
            assert "Go SDK" in sdk_names
            assert "Node.js/npm" in sdk_names

    @pytest.mark.asyncio
    async def test_run_sdk_diagnostics_parallel_execution(self):
        """Test that run_sdk_diagnostics executes checks in parallel."""
        service = DiagnosticsService()

        # Mock each check to take 0.2 seconds
        async def slow_communicate():
            await asyncio.sleep(0.2)
            return (b"1.0.0\n", b"")

        mock_process = Mock()
        mock_process.returncode = 0
        mock_process.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            start_time = asyncio.get_event_loop().time()
            results = await service.run_sdk_diagnostics()
            elapsed_time = asyncio.get_event_loop().time() - start_time

            # If parallel, should take ~0.2s (not 3 * 0.2s = 0.6s)
            assert elapsed_time < 0.4, f"Expected parallel execution <0.4s, got {elapsed_time}s"
            assert len(results) == 3

    @pytest.mark.asyncio
    async def test_run_sdk_diagnostics_mixed_results(self):
        """Test run_sdk_diagnostics with mixed success/failure results."""
        service = DiagnosticsService()

        # Mock: .NET works, Node.js missing, Go has error
        async def mock_exec_factory(program, *args, **kwargs):
            mock_process = Mock()

            if program == "dotnet":
                mock_process.returncode = 0
                mock_process.communicate = AsyncMock(return_value=(b"6.0.100\n", b""))
            elif program == "npm":
                raise FileNotFoundError("npm not found")
            elif program == "go":
                mock_process.returncode = 1
                mock_process.communicate = AsyncMock(return_value=(b"", b"error\n"))
            else:
                mock_process.returncode = 0
                mock_process.communicate = AsyncMock(return_value=(b"1.0.0\n", b""))

            return mock_process

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec_factory):
            results = await service.run_sdk_diagnostics()

            assert len(results) == 3

            # Check individual statuses
            dotnet_result = next((r for r in results if ".NET SDK" in r.name), None)
            nodejs_result = next((r for r in results if "Node.js" in r.name), None)
            go_result = next((r for r in results if "Go SDK" in r.name), None)

            assert dotnet_result.status == DiagnosticStatus.WORKING
            assert nodejs_result.status == DiagnosticStatus.NOT_CONFIGURED
            assert go_result.status == DiagnosticStatus.ERROR


class TestCategoryDispatch:
    """Test run_category dispatches to CLI/SDK diagnostic methods."""

    @pytest.mark.asyncio
    async def test_run_category_dispatches_cli_tools(self):
        """Test run_category dispatches CLI_TOOLS to run_cli_tool_diagnostics."""
        from code_indexer.server.services.diagnostics_service import DiagnosticCategory
        import tempfile
        import os

        # Use temporary database to avoid cache from DB
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            # Clear cache to ensure fresh run
            service.clear_cache(DiagnosticCategory.CLI_TOOLS)

            # Mock run_cli_tool_diagnostics (must be AsyncMock for async methods)
            mock_results = [
                Mock(status=DiagnosticStatus.WORKING, name="ripgrep"),
                Mock(status=DiagnosticStatus.WORKING, name="Git"),
            ]

            with patch.object(
                service, "run_cli_tool_diagnostics", new=AsyncMock(return_value=mock_results)
            ) as mock_method:
                await service.run_category(DiagnosticCategory.CLI_TOOLS)

                # Should call run_cli_tool_diagnostics
                mock_method.assert_called_once()

                # Results should be cached
                cached_results = service.get_category_status(DiagnosticCategory.CLI_TOOLS)
                assert len(cached_results) == 2
        finally:
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)

    @pytest.mark.asyncio
    async def test_run_category_dispatches_sdk_prerequisites(self):
        """Test run_category dispatches SDK_PREREQUISITES to run_sdk_diagnostics."""
        from code_indexer.server.services.diagnostics_service import DiagnosticCategory
        import tempfile
        import os

        # Use temporary database to avoid cache from DB
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        try:
            service = DiagnosticsService(db_path=tmp_db_path)

            # Clear cache to ensure fresh run
            service.clear_cache(DiagnosticCategory.SDK_PREREQUISITES)

            # Mock run_sdk_diagnostics (must be AsyncMock for async methods)
            mock_results = [
                Mock(status=DiagnosticStatus.WORKING, name=".NET SDK"),
                Mock(status=DiagnosticStatus.NOT_CONFIGURED, name="Node.js/npm"),
            ]

            with patch.object(service, "run_sdk_diagnostics", new=AsyncMock(return_value=mock_results)) as mock_method:
                await service.run_category(DiagnosticCategory.SDK_PREREQUISITES)

                # Should call run_sdk_diagnostics
                mock_method.assert_called_once()

                # Results should be cached
                cached_results = service.get_category_status(DiagnosticCategory.SDK_PREREQUISITES)
                assert len(cached_results) == 2
        finally:
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)
