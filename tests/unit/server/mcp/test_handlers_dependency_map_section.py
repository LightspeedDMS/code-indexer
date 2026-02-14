"""
Unit tests for dependency map section in quick_reference handler (Story #194).

Tests verify that:
1. AC1: Dependency map section appears when dependency-map/ directory and _index.md exist
2. AC2: Section is prominently positioned (after server_identity but before tools)
3. AC3: Section is included when dependency-map/ and _index.md exist
4. AC4: Section is NOT included when dependency-map/ directory doesn't exist
5. AC5: Section is NOT included when _index.md is missing (incomplete map)
6. AC6: Section content includes workflow steps, location, efficiency benefit, domain count
7. AC7: Follows _build_langfuse_section() pattern

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import pytest
import re
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime
from tempfile import TemporaryDirectory

from code_indexer.server.mcp.handlers import quick_reference, _build_dependency_map_section
from code_indexer.server.auth.user_manager import User, UserRole


def _extract_mcp_data(mcp_response: dict) -> dict:
    """Extract the JSON data from MCP-compliant content array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}


def _extract_domain_count_from_section(section_text: str) -> int:
    """Extract domain file count from section text for robust assertion."""
    # Look for patterns like "Available domain files: 3"
    match = re.search(r'Available domain files:\s*(\d+)', section_text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Also try "3 domain files" or "3 domains" in the text
    match = re.search(r'(\d+)\s+domain', section_text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return -1  # Not found


class TestBuildDependencyMapSection:
    """Test suite for _build_dependency_map_section function (Story #194)."""

    def test_section_returned_when_directory_and_index_exist(self):
        """AC3: Section returned when dependency-map/ directory and _index.md exist."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            # Create _index.md
            index_file = dep_map_dir / "_index.md"
            index_file.write_text("# Dependency Map Index\n")

            # Create some domain files
            (dep_map_dir / "api-gateway.md").write_text("# API Gateway\n")
            (dep_map_dir / "auth-service.md").write_text("# Auth Service\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should return a non-empty string (section content)
            assert result != ""
            assert isinstance(result, str)

    def test_empty_string_when_directory_missing(self):
        """AC4: Empty string returned when dependency-map/ directory doesn't exist."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            # No dependency-map/ directory created

            result = _build_dependency_map_section(cidx_meta_path)

            assert result == ""

    def test_empty_string_when_index_missing(self):
        """AC5: Empty string when dependency-map/ exists but _index.md missing."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            # Create domain files but NO _index.md
            (dep_map_dir / "api-gateway.md").write_text("# API Gateway\n")
            (dep_map_dir / "auth-service.md").write_text("# Auth Service\n")

            result = _build_dependency_map_section(cidx_meta_path)

            assert result == ""

    def test_correct_domain_count(self):
        """AC6: Section includes correct count of domain files."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            # Create _index.md
            (dep_map_dir / "_index.md").write_text("# Index\n")

            # Create domain files
            (dep_map_dir / "api-gateway.md").write_text("# API Gateway\n")
            (dep_map_dir / "auth-service.md").write_text("# Auth Service\n")
            (dep_map_dir / "database.md").write_text("# Database\n")

            # Create hidden file (should be excluded)
            (dep_map_dir / "_hidden.md").write_text("# Hidden\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Extract and verify exact count
            count = _extract_domain_count_from_section(result)
            assert count == 3, f"Expected 3 domain files, found {count} in: {result}"

    def test_section_contains_workflow_steps(self):
        """AC6: Section contains numbered workflow steps."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should contain workflow steps (numbered)
            assert "1" in result or "step" in result.lower() or "workflow" in result.lower()

    def test_section_contains_location_info(self):
        """AC6: Section explains where dependency map lives."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention cidx-meta and dependency-map directory
            assert "cidx-meta" in result or "dependency-map" in result

    def test_section_contains_efficiency_benefit(self):
        """AC6: Section mentions efficiency benefit."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention efficiency or similar concept
            assert "efficient" in result.lower() or "faster" in result.lower() or "first" in result.lower()

    def test_hidden_files_excluded_from_count(self):
        """AC6: Files starting with _ are excluded from domain count."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "_template.md").write_text("# Template\n")
            (dep_map_dir / "_hidden.md").write_text("# Hidden\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")
            (dep_map_dir / "domain2.md").write_text("# Domain 2\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Extract and verify exact count
            count = _extract_domain_count_from_section(result)
            assert count == 2, f"Expected 2 domain files, found {count} in: {result}"

    def test_non_md_files_excluded(self):
        """AC6: Only .md files are counted as domain files."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")
            (dep_map_dir / "notes.txt").write_text("Notes\n")
            (dep_map_dir / "README").write_text("README\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Extract and verify exact count
            count = _extract_domain_count_from_section(result)
            assert count == 1, f"Expected 1 domain file, found {count} in: {result}"


class TestQuickReferenceDependencyMapIntegration:
    """Test integration of dependency map section into quick_reference handler."""

    @pytest.fixture
    def test_user(self):
        """Create a test user with query permissions."""
        return User(
            username="test",
            password_hash="hashed_password",
            role=UserRole.POWER_USER,
            created_at=datetime.now(),
        )

    def test_section_included_when_dependency_map_exists(self, test_user):
        """AC3: quick_reference includes dependency map section when available."""
        with TemporaryDirectory() as tmpdir:
            # Setup cidx-meta with dependency-map
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir(parents=True)

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            # Mock config and golden_repos_dir
            mock_config = MagicMock()
            mock_config.service_display_name = "Neo"
            mock_config.langfuse_config = None  # Disable Langfuse section

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_get_service, \
                 patch("code_indexer.server.mcp.handlers.app_module") as mock_app:

                mock_service = MagicMock()
                mock_service.get_config.return_value = mock_config
                mock_get_service.return_value = mock_service

                # Mock golden_repo_manager to provide golden_repos_dir
                mock_grm = MagicMock()
                mock_grm.golden_repos_dir = tmpdir
                mock_app.golden_repo_manager = mock_grm

                mcp_response = quick_reference({}, test_user)

            result = _extract_mcp_data(mcp_response)

            # Should include dependency_map section
            assert result["success"] is True
            assert "dependency_map" in result
            assert result["dependency_map"] != ""

    def test_section_positioned_prominently(self, test_user):
        """AC2: Dependency map section is prominently positioned after server_identity."""
        with TemporaryDirectory() as tmpdir:
            # Setup cidx-meta with dependency-map
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir(parents=True)

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            mock_config = MagicMock()
            mock_config.service_display_name = "Neo"
            mock_config.langfuse_config = None

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_get_service, \
                 patch("code_indexer.server.mcp.handlers.app_module") as mock_app:

                mock_service = MagicMock()
                mock_service.get_config.return_value = mock_config
                mock_get_service.return_value = mock_service

                mock_grm = MagicMock()
                mock_grm.golden_repos_dir = tmpdir
                mock_app.golden_repo_manager = mock_grm

                mcp_response = quick_reference({}, test_user)

            result = _extract_mcp_data(mcp_response)

            # Verify ordering: server_identity exists, dependency_map exists and appears in response
            keys = list(result.keys())
            assert "dependency_map" in keys
            assert "server_identity" in keys

            # dependency_map should be present in the response (positioning verified by presence)
            # The exact position may vary based on dict insertion order, but it should appear
            dep_map_idx = keys.index("dependency_map")
            server_identity_idx = keys.index("server_identity")

            # dependency_map should be after server_identity (prominent positioning)
            assert dep_map_idx > server_identity_idx

    def test_section_not_included_when_directory_missing(self, test_user):
        """AC4: quick_reference excludes dependency map when directory missing."""
        with TemporaryDirectory() as tmpdir:
            # Setup cidx-meta WITHOUT dependency-map directory
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            cidx_meta_path.mkdir(parents=True)

            mock_config = MagicMock()
            mock_config.service_display_name = "Neo"
            mock_config.langfuse_config = None

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_get_service, \
                 patch("code_indexer.server.mcp.handlers.app_module") as mock_app:

                mock_service = MagicMock()
                mock_service.get_config.return_value = mock_config
                mock_get_service.return_value = mock_service

                mock_grm = MagicMock()
                mock_grm.golden_repos_dir = tmpdir
                mock_app.golden_repo_manager = mock_grm

                mcp_response = quick_reference({}, test_user)

            result = _extract_mcp_data(mcp_response)

            # Should NOT include dependency_map section
            assert result["success"] is True
            assert "dependency_map" not in result

    def test_section_not_included_when_index_missing(self, test_user):
        """AC5: quick_reference excludes dependency map when _index.md missing."""
        with TemporaryDirectory() as tmpdir:
            # Setup cidx-meta with dependency-map but NO _index.md
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir(parents=True)

            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")
            # NO _index.md

            mock_config = MagicMock()
            mock_config.service_display_name = "Neo"
            mock_config.langfuse_config = None

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_get_service, \
                 patch("code_indexer.server.mcp.handlers.app_module") as mock_app:

                mock_service = MagicMock()
                mock_service.get_config.return_value = mock_config
                mock_get_service.return_value = mock_service

                mock_grm = MagicMock()
                mock_grm.golden_repos_dir = tmpdir
                mock_app.golden_repo_manager = mock_grm

                mcp_response = quick_reference({}, test_user)

            result = _extract_mcp_data(mcp_response)

            # Should NOT include dependency_map section
            assert result["success"] is True
            assert "dependency_map" not in result

    def test_no_error_when_dependency_map_missing(self, test_user):
        """AC4/AC5: No error raised when dependency map is missing."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            cidx_meta_path.mkdir(parents=True)

            mock_config = MagicMock()
            mock_config.service_display_name = "Neo"
            mock_config.langfuse_config = None

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_get_service, \
                 patch("code_indexer.server.mcp.handlers.app_module") as mock_app:

                mock_service = MagicMock()
                mock_service.get_config.return_value = mock_config
                mock_get_service.return_value = mock_service

                mock_grm = MagicMock()
                mock_grm.golden_repos_dir = tmpdir
                mock_app.golden_repo_manager = mock_grm

                # Should not raise an error
                mcp_response = quick_reference({}, test_user)

            result = _extract_mcp_data(mcp_response)
            assert result["success"] is True
            assert "error" not in result

    def test_other_sections_unchanged_when_dependency_map_missing(self, test_user):
        """AC4: Other quick reference sections remain when dependency map missing."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir) / "cidx-meta"
            cidx_meta_path.mkdir(parents=True)

            mock_config = MagicMock()
            mock_config.service_display_name = "Neo"
            mock_config.langfuse_config = None

            with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_get_service, \
                 patch("code_indexer.server.mcp.handlers.app_module") as mock_app:

                mock_service = MagicMock()
                mock_service.get_config.return_value = mock_config
                mock_get_service.return_value = mock_service

                mock_grm = MagicMock()
                mock_grm.golden_repos_dir = tmpdir
                mock_app.golden_repo_manager = mock_grm

                mcp_response = quick_reference({}, test_user)

            result = _extract_mcp_data(mcp_response)

            # Core sections should still be present
            assert result["success"] is True
            assert "server_identity" in result
            assert "total_tools" in result
            assert "tools" in result
