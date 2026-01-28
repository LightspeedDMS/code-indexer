"""Unit tests for SCIP MCP handlers golden repo integration.

Tests verify that SCIP handlers search golden repos directory instead of Path.cwd()
and handle missing SCIP indexes appropriately.

Story #40: After refactoring, handlers use SCIPQueryService instead of _find_scip_files.
These tests now verify SCIPQueryService.find_scip_files() directly.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from code_indexer.server.services.scip_query_service import SCIPQueryService


class TestSCIPQueryServiceFindScipFiles:
    """Tests for SCIPQueryService.find_scip_files() golden repos directory search.

    Story #40: These tests were updated to test SCIPQueryService directly
    instead of the removed _find_scip_files function from handlers.py.
    """

    def test_find_scip_files_searches_golden_repos(self, tmp_path: Path) -> None:
        """Verify SCIPQueryService.find_scip_files() searches golden repos directory."""
        # Setup: Create mock golden repos structure
        golden_repos_dir = tmp_path / "golden-repos"
        repo1_scip = golden_repos_dir / "repo1" / ".code-indexer" / "scip"
        repo2_scip = golden_repos_dir / "repo2" / ".code-indexer" / "scip"
        repo1_scip.mkdir(parents=True)
        repo2_scip.mkdir(parents=True)

        # Create mock .scip.db files (new format)
        scip_file1 = repo1_scip / "index.scip.db"
        scip_file2 = repo2_scip / "index.scip.db"
        scip_file1.write_text("mock scip data 1")
        scip_file2.write_text("mock scip data 2")

        # Create service with our test directory
        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)

        # Execute
        scip_files = service.find_scip_files()

        # Verify: Should find both .scip.db files from golden repos
        assert len(scip_files) == 2
        scip_file_paths = {str(f) for f in scip_files}
        assert str(scip_file1) in scip_file_paths
        assert str(scip_file2) in scip_file_paths

    def test_find_scip_files_returns_empty_when_golden_repos_dir_not_exists(
        self, tmp_path: Path
    ) -> None:
        """Verify SCIPQueryService.find_scip_files() returns empty list when dir doesn't exist."""
        nonexistent_dir = tmp_path / "nonexistent"

        # Create service with nonexistent directory
        service = SCIPQueryService(golden_repos_dir=nonexistent_dir)

        # Execute
        scip_files = service.find_scip_files()

        # Verify: Should return empty list
        assert scip_files == []

    def test_find_scip_files_handles_nested_scip_files(self, tmp_path: Path) -> None:
        """Verify SCIPQueryService.find_scip_files() finds nested .scip.db files."""
        # Setup: Create nested structure
        golden_repos_dir = tmp_path / "golden-repos"
        repo_scip = golden_repos_dir / "repo1" / ".code-indexer" / "scip"
        nested_dir = repo_scip / "subdir"
        nested_dir.mkdir(parents=True)

        # Create .scip.db files at different levels
        scip_file1 = repo_scip / "index.scip.db"
        scip_file2 = nested_dir / "nested.scip.db"
        scip_file1.write_text("mock scip data 1")
        scip_file2.write_text("mock scip data 2")

        # Create service
        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)

        # Execute
        scip_files = service.find_scip_files()

        # Verify: Should find both files
        assert len(scip_files) == 2

    def test_find_scip_files_ignores_non_directory_entries(
        self, tmp_path: Path
    ) -> None:
        """Verify SCIPQueryService.find_scip_files() skips non-directory entries."""
        # Setup: Create golden repos with file and directory
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        # Create a file (should be ignored)
        file_entry = golden_repos_dir / "somefile.txt"
        file_entry.write_text("not a repo")

        # Create a valid repo directory
        repo_scip = golden_repos_dir / "repo1" / ".code-indexer" / "scip"
        repo_scip.mkdir(parents=True)
        scip_file = repo_scip / "index.scip.db"
        scip_file.write_text("mock scip data")

        # Create service
        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)

        # Execute
        scip_files = service.find_scip_files()

        # Verify: Should only find the one valid .scip.db file
        assert len(scip_files) == 1
        assert scip_files[0] == scip_file

    def test_find_scip_files_filters_by_repository_alias(self, tmp_path: Path) -> None:
        """Verify SCIPQueryService.find_scip_files(repository_alias) filters correctly."""
        # Setup: Create mock golden repos structure with multiple repos
        golden_repos_dir = tmp_path / "golden-repos"
        repo1_scip = golden_repos_dir / "repo1" / ".code-indexer" / "scip"
        repo2_scip = golden_repos_dir / "repo2" / ".code-indexer" / "scip"
        repo1_scip.mkdir(parents=True)
        repo2_scip.mkdir(parents=True)

        # Create mock .scip.db files (new format)
        scip_file1 = repo1_scip / "index.scip.db"
        scip_file2 = repo2_scip / "index.scip.db"
        scip_file1.write_text("mock scip data 1")
        scip_file2.write_text("mock scip data 2")

        # Create service
        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)

        # Execute: Query with repository_alias="repo1"
        scip_files = service.find_scip_files(repository_alias="repo1")

        # Verify: Should only find repo1's .scip.db file, not repo2's
        assert len(scip_files) == 1
        assert scip_files[0] == scip_file1
        assert scip_file2 not in scip_files


class TestScipHandlersErrorHandling:
    """Tests for SCIP handlers error handling when no indexes exist.

    Story #40: Updated to mock _get_scip_query_service instead of _find_scip_files.
    Handlers now delegate to SCIPQueryService for SCIP file discovery.
    """

    def _create_mock_service_returning_empty(self) -> MagicMock:
        """Create a mock SCIPQueryService that returns empty results."""
        mock_service = MagicMock()
        mock_service.find_scip_files.return_value = []
        mock_service.find_definition.return_value = []
        mock_service.find_references.return_value = []
        mock_service.get_dependencies.return_value = []
        mock_service.get_dependents.return_value = []
        mock_service.analyze_impact.return_value = {
            "target_symbol": "",
            "depth_analyzed": 0,
            "total_affected": 0,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }
        mock_service.trace_callchain.return_value = []
        mock_service.get_context.return_value = {
            "target_symbol": "",
            "summary": "",
            "files": [],
            "total_files": 0,
            "total_symbols": 0,
            "avg_relevance": 0.0,
        }
        return mock_service

    def test_scip_definition_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_definition returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function"}
            result = scip_definition(params, mock_user)

            # Verify: Should return success with empty results
            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_results"] == 0
            assert data["results"] == []

    def test_scip_definition_passes_repository_alias_to_service(self) -> None:
        """Verify scip_definition passes repository_alias parameter to service."""
        from code_indexer.server.mcp.handlers import scip_definition

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function", "repository_alias": "test-repo"}
            scip_definition(params, mock_user)

            # Verify service.find_definition was called with repository_alias
            mock_service.find_definition.assert_called_once_with(
                symbol="some_function",
                exact=False,
                repository_alias="test-repo",
                username="testuser",
            )

    def test_scip_references_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_references returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_references

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function"}
            result = scip_references(params, mock_user)

            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_results"] == 0

    def test_scip_references_passes_repository_alias_to_service(self) -> None:
        """Verify scip_references passes repository_alias parameter to service."""
        from code_indexer.server.mcp.handlers import scip_references

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function", "repository_alias": "test-repo"}
            scip_references(params, mock_user)

            # Verify service.find_references was called with repository_alias
            mock_service.find_references.assert_called_once()
            call_kwargs = mock_service.find_references.call_args[1]
            assert call_kwargs["repository_alias"] == "test-repo"
            assert call_kwargs["username"] == "testuser"

    def test_scip_dependencies_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_dependencies returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_dependencies

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function"}
            result = scip_dependencies(params, mock_user)

            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_results"] == 0

    def test_scip_dependencies_passes_repository_alias_to_service(self) -> None:
        """Verify scip_dependencies passes repository_alias parameter to service."""
        from code_indexer.server.mcp.handlers import scip_dependencies

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function", "repository_alias": "test-repo"}
            scip_dependencies(params, mock_user)

            # Verify service.get_dependencies was called with repository_alias
            mock_service.get_dependencies.assert_called_once()
            call_kwargs = mock_service.get_dependencies.call_args[1]
            assert call_kwargs["repository_alias"] == "test-repo"
            assert call_kwargs["username"] == "testuser"

    def test_scip_dependents_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_dependents returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_dependents

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function"}
            result = scip_dependents(params, mock_user)

            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_results"] == 0

    def test_scip_dependents_passes_repository_alias_to_service(self) -> None:
        """Verify scip_dependents passes repository_alias parameter to service."""
        from code_indexer.server.mcp.handlers import scip_dependents

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function", "repository_alias": "test-repo"}
            scip_dependents(params, mock_user)

            # Verify service.get_dependents was called with repository_alias
            mock_service.get_dependents.assert_called_once()
            call_kwargs = mock_service.get_dependents.call_args[1]
            assert call_kwargs["repository_alias"] == "test-repo"
            assert call_kwargs["username"] == "testuser"

    def test_scip_impact_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_impact returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_impact

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function"}
            result = scip_impact(params, mock_user)

            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_affected"] == 0

    def test_scip_callchain_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_callchain returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"from_symbol": "func1", "to_symbol": "func2"}
            result = scip_callchain(params, mock_user)

            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_chains_found"] == 0

    def test_scip_callchain_passes_repository_alias_to_service(self) -> None:
        """Verify scip_callchain passes repository_alias parameter to service."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {
                "from_symbol": "func1",
                "to_symbol": "func2",
                "repository_alias": "test-repo",
            }
            scip_callchain(params, mock_user)

            # Verify service.trace_callchain was called with repository_alias
            mock_service.trace_callchain.assert_called_once()
            call_kwargs = mock_service.trace_callchain.call_args[1]
            assert call_kwargs["repository_alias"] == "test-repo"
            assert call_kwargs["username"] == "testuser"

    def test_scip_context_returns_empty_when_no_indexes(self) -> None:
        """Verify scip_context returns empty results when service finds no indexes."""
        from code_indexer.server.mcp.handlers import scip_context

        mock_service = self._create_mock_service_returning_empty()
        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            params = {"symbol": "some_function"}
            result = scip_context(params, mock_user)

            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_files"] == 0


class TestScipCompositeHandlersGoldenReposDirectory:
    """Tests verifying composite handlers delegate to SCIPQueryService.

    Story #40: Updated to mock _get_scip_query_service and verify handlers
    delegate to service methods with correct parameters.
    """

    def test_scip_impact_delegates_to_service(self) -> None:
        """Verify scip_impact delegates to SCIPQueryService.analyze_impact()."""
        from code_indexer.server.mcp.handlers import scip_impact

        # Create mock service with expected response
        mock_service = MagicMock()
        mock_service.analyze_impact.return_value = {
            "target_symbol": "test",
            "depth_analyzed": 2,
            "total_affected": 0,
            "truncated": False,
            "affected_symbols": [],
            "affected_files": [],
        }

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            result = scip_impact({"symbol": "test", "depth": 2}, mock_user)

            # Verify service.analyze_impact was called with correct params
            mock_service.analyze_impact.assert_called_once_with(
                symbol="test",
                depth=2,
                repository_alias=None,
                username="testuser",
            )

            # Verify response
            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["target_symbol"] == "test"
            assert data["depth_analyzed"] == 2

    def test_scip_callchain_delegates_to_service(self) -> None:
        """Verify scip_callchain delegates to SCIPQueryService.trace_callchain()."""
        from code_indexer.server.mcp.handlers import scip_callchain

        # Create mock service with expected response
        mock_service = MagicMock()
        mock_service.trace_callchain.return_value = [
            {"path": ["func1", "intermediate", "func2"], "length": 3, "has_cycle": False}
        ]

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            result = scip_callchain(
                {"from_symbol": "func1", "to_symbol": "func2"}, mock_user
            )

            # Verify service.trace_callchain was called
            mock_service.trace_callchain.assert_called_once()
            call_kwargs = mock_service.trace_callchain.call_args[1]
            assert call_kwargs["from_symbol"] == "func1"
            assert call_kwargs["to_symbol"] == "func2"
            assert call_kwargs["username"] == "testuser"

            # Verify response
            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_chains_found"] == 1

    def test_scip_callchain_clamps_max_depth_to_10(self) -> None:
        """Verify scip_callchain clamps max_depth to 10 when user passes value > 10.

        Bug: User passes max_depth=15 via MCP, handler passes it unclamped to
        service, which may raise ValueError because it only accepts max_depth <= 10.

        Fix: Handler should validate/clamp max_depth to [1, 10] range before calling
        service to provide early validation and clearer error handling.
        """
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_service = MagicMock()
        mock_service.trace_callchain.return_value = []

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            # Execute with max_depth=15 (exceeds limit)
            result = scip_callchain(
                {"from_symbol": "func1", "to_symbol": "func2", "max_depth": 15},
                mock_user,
            )

            # Should succeed (no exception)
            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True

            # Verify service.trace_callchain was called with clamped max_depth <= 10
            mock_service.trace_callchain.assert_called_once()
            call_kwargs = mock_service.trace_callchain.call_args[1]
            max_depth_arg = call_kwargs.get("max_depth")
            assert (
                max_depth_arg <= 10
            ), f"Expected max_depth <= 10, got {max_depth_arg}"

    def test_scip_context_delegates_to_service(self) -> None:
        """Verify scip_context delegates to SCIPQueryService.get_context()."""
        from code_indexer.server.mcp.handlers import scip_context

        # Create mock service with expected response
        mock_service = MagicMock()
        mock_service.get_context.return_value = {
            "target_symbol": "test",
            "summary": "Test summary",
            "files": [],
            "total_files": 0,
            "total_symbols": 0,
            "avg_relevance": 0.0,
        }

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            result = scip_context({"symbol": "test"}, mock_user)

            # Verify service.get_context was called with correct params
            mock_service.get_context.assert_called_once()
            call_kwargs = mock_service.get_context.call_args[1]
            assert call_kwargs["symbol"] == "test"
            assert call_kwargs["username"] == "testuser"

            # Verify response
            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["target_symbol"] == "test"
            assert data["total_files"] == 0


class TestScipCallchainSymbolValidation:
    """Tests for scip_callchain symbol format validation."""

    def test_scip_callchain_validates_empty_from_symbol(self) -> None:
        """Verify scip_callchain returns error when from_symbol is empty."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_user = MagicMock()
        params = {"from_symbol": "", "to_symbol": "valid_symbol"}
        result = scip_callchain(params, mock_user)

        content = result.get("content", [])
        assert len(content) > 0
        data = json.loads(content[0]["text"])
        assert data["success"] is False
        assert "from_symbol" in data["error"].lower()
        assert (
            "empty" in data["error"].lower()
            or "cannot be empty" in data["error"].lower()
        )

    def test_scip_callchain_validates_empty_to_symbol(self) -> None:
        """Verify scip_callchain returns error when to_symbol is empty."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_user = MagicMock()
        params = {"from_symbol": "valid_symbol", "to_symbol": ""}
        result = scip_callchain(params, mock_user)

        content = result.get("content", [])
        assert len(content) > 0
        data = json.loads(content[0]["text"])
        assert data["success"] is False
        assert "to_symbol" in data["error"].lower()
        assert (
            "empty" in data["error"].lower()
            or "cannot be empty" in data["error"].lower()
        )

    def test_scip_callchain_validates_whitespace_from_symbol(self) -> None:
        """Verify scip_callchain returns error when from_symbol is only whitespace."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_user = MagicMock()
        params = {"from_symbol": "   ", "to_symbol": "valid_symbol"}
        result = scip_callchain(params, mock_user)

        content = result.get("content", [])
        assert len(content) > 0
        data = json.loads(content[0]["text"])
        assert data["success"] is False
        assert "from_symbol" in data["error"].lower()
        assert (
            "empty" in data["error"].lower()
            or "cannot be empty" in data["error"].lower()
        )

    def test_scip_callchain_validates_whitespace_to_symbol(self) -> None:
        """Verify scip_callchain returns error when to_symbol is only whitespace."""
        from code_indexer.server.mcp.handlers import scip_callchain

        mock_user = MagicMock()
        params = {"from_symbol": "valid_symbol", "to_symbol": "   "}
        result = scip_callchain(params, mock_user)

        content = result.get("content", [])
        assert len(content) > 0
        data = json.loads(content[0]["text"])
        assert data["success"] is False
        assert "to_symbol" in data["error"].lower()
        assert (
            "empty" in data["error"].lower()
            or "cannot be empty" in data["error"].lower()
        )


class TestScipCallchainEnhancedResponse:
    """Tests for enhanced scip_callchain response format with diagnostics.

    Story #40: Updated to mock _get_scip_query_service instead of _find_scip_files.
    """

    def test_scip_callchain_includes_diagnostic_when_no_chains_found(self) -> None:
        """Verify scip_callchain includes diagnostic message when 0 chains found."""
        from code_indexer.server.mcp.handlers import scip_callchain

        # Create mock service that returns empty chains
        mock_service = MagicMock()
        mock_service.trace_callchain.return_value = []

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            result = scip_callchain(
                {"from_symbol": "func1", "to_symbol": "func2"}, mock_user
            )

            # Verify response includes diagnostic information
            content = result.get("content", [])
            assert len(content) > 0
            data = json.loads(content[0]["text"])
            assert data["success"] is True
            assert data["total_chains_found"] == 0
            assert "scip_files_searched" in data
            assert data["scip_files_searched"] >= 0
            assert "repository_filter" in data
            assert "diagnostic" in data
            assert data["diagnostic"] is not None
            assert "No call chains found" in data["diagnostic"]
            assert "func1" in data["diagnostic"]
            assert "func2" in data["diagnostic"]


class TestScipHandlerRegistration:
    """Tests for SCIP handler registration in HANDLER_REGISTRY."""

    def test_scip_handlers_registered_in_handler_registry(self) -> None:
        """Verify all 7 SCIP handlers are registered in HANDLER_REGISTRY.

        This test prevents regression of the bug where SCIP handlers were defined
        but not registered, causing "Handler not implemented" errors.
        """
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        expected_handlers = [
            "scip_definition",
            "scip_references",
            "scip_dependencies",
            "scip_dependents",
            "scip_impact",
            "scip_callchain",
            "scip_context",
        ]

        for handler_name in expected_handlers:
            assert (
                handler_name in HANDLER_REGISTRY
            ), f"Handler '{handler_name}' not registered in HANDLER_REGISTRY"
            assert callable(
                HANDLER_REGISTRY[handler_name]
            ), f"Handler '{handler_name}' is not callable"
