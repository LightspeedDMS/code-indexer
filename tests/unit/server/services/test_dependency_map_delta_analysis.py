"""
Unit tests for DependencyMapService delta analysis orchestration (Story #193).

Tests the full delta analysis pipeline:
- Skipping when no changes detected
- In-place domain file updates (not stage-then-swap)
- Tracking table updates on success and failure
- CLAUDE.md cleanup
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


@pytest.fixture
def tmp_golden_repos_root(tmp_path: Path) -> Path:
    """Create temporary golden repos root directory structure."""
    golden_repos_root = tmp_path / "golden-repos"
    golden_repos_root.mkdir()

    # Create cidx-meta directory
    cidx_meta = golden_repos_root / "cidx-meta"
    cidx_meta.mkdir()

    # Create sample repo directories
    for alias in ["repo1", "repo2"]:
        repo_dir = golden_repos_root / alias
        repo_dir.mkdir()
        code_indexer_dir = repo_dir / ".code-indexer"
        code_indexer_dir.mkdir()

        metadata = {
            "current_commit": f"{alias}-commit-abc123",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        (code_indexer_dir / "metadata.json").write_text(json.dumps(metadata))

    # Create dependency-map directory
    dep_map_dir = cidx_meta / "dependency-map"
    dep_map_dir.mkdir()

    # Create _index.md (both repos in authentication domain)
    index_content = """---
schema_version: 1.0
repos_analyzed:
  - repo1
  - repo2
---

# Dependency Map Index

## Repo-to-Domain Matrix

| Repository | Domains |
|------------|---------|
| repo1 | authentication |
| repo2 | authentication |
"""
    (dep_map_dir / "_index.md").write_text(index_content)

    # Create existing domain file
    auth_domain = """---
domain: authentication
last_analyzed: 2024-01-01T00:00:00Z
participating_repos:
  - repo1
---

# Authentication Domain

Old analysis content here.
"""
    (dep_map_dir / "authentication.md").write_text(auth_domain)

    return golden_repos_root


@pytest.fixture
def mock_tracking_backend():
    """Create mock tracking backend."""
    backend = Mock()
    backend.get_tracking.return_value = {
        "id": 1,
        "last_run": "2024-01-01T00:00:00Z",
        "next_run": "2024-01-02T00:00:00Z",
        "status": "completed",
        "commit_hashes": json.dumps({
            "repo1": "repo1-commit-abc123",
            "repo2": "repo2-commit-old456",  # Changed
        }),
        "error_message": None,
    }
    return backend


@pytest.fixture
def mock_golden_repos_manager(tmp_golden_repos_root: Path):
    """Create mock golden repos manager."""
    manager = Mock()
    manager.golden_repos_dir = tmp_golden_repos_root
    manager.list_golden_repos.return_value = [
        {"alias": "repo1", "clone_path": str(tmp_golden_repos_root / "repo1")},
        {"alias": "repo2", "clone_path": str(tmp_golden_repos_root / "repo2")},
    ]
    return manager


@pytest.fixture
def dependency_map_service(
    mock_golden_repos_manager,
    mock_tracking_backend,
):
    """Create DependencyMapService instance."""
    config_manager = Mock()
    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
        dependency_map_delta_max_turns=30,
    )
    config_manager.get_claude_integration_config.return_value = config

    return DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=Mock(),
    )


class TestTrackingTableUpdates:
    """Test tracking table updates after delta refresh (AC8)."""

    @patch("subprocess.run")
    @patch("code_indexer.global_repos.dependency_map_analyzer.ClaudeCliManager")
    def test_tracking_updated_on_successful_delta(
        self, mock_claude_manager_class, mock_subprocess, dependency_map_service,
        mock_tracking_backend
    ):
        """Test that tracking table is updated on successful delta refresh."""
        # Mock Claude CLI invocation
        mock_claude_manager_class.sync_api_key = staticmethod(lambda: None)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Updated analysis")

        # Trigger delta analysis
        dependency_map_service.run_delta_analysis()

        # Check that tracking was updated
        update_calls = mock_tracking_backend.update_tracking.call_args_list
        assert len(update_calls) > 0

        # Find the final update call (should have status=completed)
        final_update = None
        for call in update_calls:
            args, kwargs = call
            if kwargs.get("status") == "completed":
                final_update = kwargs
                break

        assert final_update is not None
        assert "commit_hashes" in final_update
        assert "next_run" in final_update

    def test_tracking_updated_on_failed_delta(
        self, dependency_map_service, mock_tracking_backend
    ):
        """Test that tracking table is updated with error on failed delta."""
        # Force an error by providing invalid state
        with patch.object(
            dependency_map_service, "detect_changes", side_effect=Exception("Test error")
        ):
            # Attempt delta analysis (should update tracking then re-raise)
            with pytest.raises(Exception, match="Test error"):
                dependency_map_service.run_delta_analysis()

        # Check that tracking was updated with error
        update_calls = mock_tracking_backend.update_tracking.call_args_list
        assert len(update_calls) > 0

        # Find error update
        error_update = None
        for call in update_calls:
            args, kwargs = call
            if kwargs.get("status") == "failed" or kwargs.get("error_message") is not None:
                error_update = kwargs
                break

        assert error_update is not None


class TestDeltaAnalysisOrchestration:
    """Test the full delta analysis orchestration flow."""

    def test_run_delta_analysis_skips_when_no_changes(
        self, mock_tracking_backend, mock_golden_repos_manager
    ):
        """Test that delta analysis skips and updates next_run when no changes detected."""
        # Set stored hashes to match current (no changes)
        mock_tracking_backend.get_tracking.return_value["commit_hashes"] = json.dumps({
            "repo1": "repo1-commit-abc123",
            "repo2": "repo2-commit-abc123",
        })

        config_manager = Mock()
        config = ClaudeIntegrationConfig(
            dependency_map_enabled=True,
            dependency_map_interval_hours=24,
        )
        config_manager.get_claude_integration_config.return_value = config

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=Mock(),
        )

        # Mock subprocess to verify Claude CLI is NOT called
        with patch("subprocess.run") as mock_subprocess:
            # Run delta analysis
            service.run_delta_analysis()

            # Should have updated next_run without running analysis
            update_calls = mock_tracking_backend.update_tracking.call_args_list
            assert len(update_calls) > 0

            # Should NOT have invoked Claude CLI
            mock_subprocess.assert_not_called()

    @patch("subprocess.run")
    @patch("code_indexer.global_repos.dependency_map_analyzer.ClaudeCliManager")
    def test_run_delta_analysis_updates_files_in_place(
        self, mock_claude_manager_class, mock_subprocess, dependency_map_service,
        tmp_golden_repos_root
    ):
        """Test that delta analysis updates domain files in-place (AC5)."""
        # Mock Claude CLI invocation
        mock_claude_manager_class.sync_api_key = staticmethod(lambda: None)
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="# Authentication Domain\n\nNew updated content."
        )

        auth_file = tmp_golden_repos_root / "cidx-meta" / "dependency-map" / "authentication.md"
        staging_dir = tmp_golden_repos_root / "cidx-meta" / "dependency-map.staging"

        # Verify file exists before
        assert auth_file.exists()
        original_content = auth_file.read_text()

        # Run delta analysis
        dependency_map_service.run_delta_analysis()

        # Verify file was updated in-place (not via staging)
        assert auth_file.exists()
        updated_content = auth_file.read_text()
        assert updated_content != original_content

        # Verify NO staging directory was created (delta uses in-place updates)
        assert not staging_dir.exists()

    @patch("subprocess.run")
    @patch("code_indexer.global_repos.dependency_map_analyzer.ClaudeCliManager")
    def test_run_delta_analysis_cleans_up_claude_md(
        self, mock_claude_manager_class, mock_subprocess, dependency_map_service,
        tmp_golden_repos_root
    ):
        """Test that CLAUDE.md is cleaned up after delta analysis."""
        # Mock Claude CLI invocation
        mock_claude_manager_class.sync_api_key = staticmethod(lambda: None)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Updated")

        claude_md = tmp_golden_repos_root / "CLAUDE.md"

        # Run delta analysis
        dependency_map_service.run_delta_analysis()

        # CLAUDE.md should be cleaned up (not exist after completion)
        assert not claude_md.exists()

    def test_run_delta_analysis_disabled_returns_early(
        self, mock_tracking_backend
    ):
        """Test that delta analysis returns early when disabled."""
        # Disable feature
        config_manager = Mock()
        config = ClaudeIntegrationConfig(dependency_map_enabled=False)
        config_manager.get_claude_integration_config.return_value = config

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=Mock(),
        )

        # Run delta analysis
        service.run_delta_analysis()

        # Should return early (no tracking updates)
        mock_tracking_backend.update_tracking.assert_not_called()

    @patch("subprocess.run")
    @patch("code_indexer.global_repos.dependency_map_analyzer.ClaudeCliManager")
    def test_domain_list_includes_all_domains_not_just_affected(
        self, mock_claude_manager_class, mock_subprocess, tmp_golden_repos_root,
        mock_golden_repos_manager, mock_tracking_backend
    ):
        """Test that domain_list passed to analyzer includes ALL domains, not just affected (H2)."""
        # Create multiple domain files in dependency-map
        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        dep_map_dir = cidx_meta / "dependency-map"

        # Add data-processing domain file (not affected by changes)
        data_domain = """---
domain: data-processing
participating_repos:
  - repo3
---

# Data Processing Domain

Unaffected domain.
"""
        (dep_map_dir / "data-processing.md").write_text(data_domain)

        # Add frontend domain file (not affected by changes)
        frontend_domain = """---
domain: frontend
participating_repos:
  - repo4
---

# Frontend Domain

Another unaffected domain.
"""
        (dep_map_dir / "frontend.md").write_text(frontend_domain)

        # Mock Claude CLI
        mock_claude_manager_class.sync_api_key = staticmethod(lambda: None)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="Updated content")

        # Setup service
        config_manager = Mock()
        config = ClaudeIntegrationConfig(
            dependency_map_enabled=True,
            dependency_map_interval_hours=24,
            dependency_map_pass_timeout_seconds=300,
            dependency_map_delta_max_turns=30,
        )
        config_manager.get_claude_integration_config.return_value = config

        # Create service with mock analyzer to capture calls
        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "Test prompt"
        mock_analyzer.invoke_delta_merge.return_value = "Updated analysis"

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        # Run delta analysis (repo2 changed)
        service.run_delta_analysis()

        # Verify build_delta_merge_prompt was called (explicit assertion prevents silent pass)
        assert mock_analyzer.build_delta_merge_prompt.called, \
            "Expected build_delta_merge_prompt to be called for delta analysis"

        call_args = mock_analyzer.build_delta_merge_prompt.call_args
        domain_list = call_args.kwargs.get("domain_list", [])

        # Should include all 3 domains: authentication, data-processing, frontend
        assert len(domain_list) >= 3, f"Expected at least 3 domains, got {len(domain_list)}: {domain_list}"
        assert "authentication" in domain_list
        assert "data-processing" in domain_list
        assert "frontend" in domain_list
