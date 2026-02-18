"""
Unit tests for DependencyMapService change detection (Story #193).

Tests change detection via commit hash comparison and affected domain identification:
- Changed repo detection (commit hash comparison)
- New repo detection (not in stored hashes)
- Removed repo detection (in stored but not current)
- Affected domain identification from _index.md parsing
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

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

    # Create sample repo directories with metadata.json
    for alias in ["repo1", "repo2", "repo3"]:
        repo_dir = golden_repos_root / alias
        repo_dir.mkdir()
        code_indexer_dir = repo_dir / ".code-indexer"
        code_indexer_dir.mkdir()

        # Create metadata.json with commit hash
        metadata = {
            "current_commit": f"{alias}-commit-abc123",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        (code_indexer_dir / "metadata.json").write_text(json.dumps(metadata))

        # Add a source file so _enrich_repo_sizes() does not filter out this repo.
        # detect_changes() now applies the same empty-repo filter as the analysis
        # pipeline: repos with 0 non-.git/.code-indexer files are excluded.
        (repo_dir / "main.py").write_text(f"# {alias} source\n")

    # Create sample repo description files in cidx-meta
    (cidx_meta / "repo1.md").write_text("# Repo 1\n\nDescription of repo 1")
    (cidx_meta / "repo2.md").write_text("# Repo 2\n\nDescription of repo 2")
    (cidx_meta / "repo3.md").write_text("# Repo 3\n\nDescription of repo 3")

    # Create dependency-map directory with _index.md
    dep_map_dir = cidx_meta / "dependency-map"
    dep_map_dir.mkdir()

    # Create _index.md with YAML frontmatter and repo-to-domain matrix
    index_content = """---
schema_version: 1.0
last_analyzed: 2024-01-01T00:00:00Z
repos_analyzed_count: 3
domains_count: 2
repos_analyzed:
  - repo1
  - repo2
  - repo3
---

# Dependency Map Index

## Domain Catalog

| Domain | Description | Repos |
|--------|-------------|-------|
| authentication | Auth domain | repo1, repo2 |
| data-processing | Data domain | repo2, repo3 |

## Repo-to-Domain Matrix

| Repository | Domains |
|------------|---------|
| repo1 | authentication |
| repo2 | authentication, data-processing |
| repo3 | data-processing |
"""
    (dep_map_dir / "_index.md").write_text(index_content)

    return golden_repos_root


@pytest.fixture
def mock_tracking_backend():
    """Create mock tracking backend with stored commit hashes."""
    backend = Mock()
    # Simulate previous analysis with stored hashes
    backend.get_tracking.return_value = {
        "id": 1,
        "last_run": "2024-01-01T00:00:00Z",
        "next_run": "2024-01-02T00:00:00Z",
        "status": "completed",
        "commit_hashes": json.dumps({
            "repo1": "repo1-commit-abc123",  # Unchanged
            "repo2": "repo2-commit-old456",  # Changed (current is abc123)
            "repo3": "repo3-commit-abc123",  # Unchanged
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
        {
            "alias": "repo1",
            "clone_path": str(tmp_golden_repos_root / "repo1"),
        },
        {
            "alias": "repo2",
            "clone_path": str(tmp_golden_repos_root / "repo2"),
        },
        {
            "alias": "repo3",
            "clone_path": str(tmp_golden_repos_root / "repo3"),
        },
    ]
    # get_actual_repo_path resolves stale clone_path to actual filesystem path;
    # in test fixtures the flat paths are valid, so return them directly
    def _resolve_path(alias: str) -> str:
        return str(tmp_golden_repos_root / alias)

    manager.get_actual_repo_path.side_effect = _resolve_path
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
    )
    config_manager.get_claude_integration_config.return_value = config

    return DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=Mock(),
    )


class TestChangeDetection:
    """Test change detection via commit hash comparison (AC2)."""

    def test_detect_changes_identifies_changed_repo(
        self, dependency_map_service, tmp_golden_repos_root
    ):
        """Test that changed repos are detected by comparing commit hashes."""
        changed, new, removed = dependency_map_service.detect_changes()

        # repo2 has changed commit hash
        assert len(changed) == 1
        assert changed[0]["alias"] == "repo2"
        assert changed[0]["clone_path"] == str(tmp_golden_repos_root / "repo2")

    def test_detect_changes_identifies_new_repo(
        self, dependency_map_service, mock_golden_repos_manager, tmp_golden_repos_root
    ):
        """Test that new repos are detected (not in stored hashes)."""
        # Add a new repo that wasn't in the previous analysis
        new_repo_dir = tmp_golden_repos_root / "repo4"
        new_repo_dir.mkdir()
        code_indexer_dir = new_repo_dir / ".code-indexer"
        code_indexer_dir.mkdir()
        metadata = {
            "current_commit": "repo4-commit-new999",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        (code_indexer_dir / "metadata.json").write_text(json.dumps(metadata))
        # Add a source file so _enrich_repo_sizes() does not filter out repo4.
        (new_repo_dir / "main.py").write_text("# repo4 source\n")

        # Update mock to include new repo
        mock_golden_repos_manager.list_golden_repos.return_value.append({
            "alias": "repo4",
            "clone_path": str(new_repo_dir),
        })

        changed, new, removed = dependency_map_service.detect_changes()

        # repo4 should be detected as new
        assert len(new) == 1
        assert new[0]["alias"] == "repo4"

    def test_detect_changes_identifies_removed_repo(
        self, dependency_map_service, mock_golden_repos_manager
    ):
        """Test that removed repos are detected (in stored but not current)."""
        # Remove repo3 from current repos (but it's in stored hashes)
        mock_golden_repos_manager.list_golden_repos.return_value = [
            repo for repo in mock_golden_repos_manager.list_golden_repos.return_value
            if repo["alias"] != "repo3"
        ]

        changed, new, removed = dependency_map_service.detect_changes()

        # repo3 should be detected as removed
        assert len(removed) == 1
        assert removed[0] == "repo3"

    def test_detect_changes_returns_empty_when_no_changes(
        self, mock_tracking_backend, mock_golden_repos_manager, tmp_golden_repos_root
    ):
        """Test that detect_changes returns empty lists when nothing changed."""
        # Set stored hashes to match current commits
        mock_tracking_backend.get_tracking.return_value["commit_hashes"] = json.dumps({
            "repo1": "repo1-commit-abc123",
            "repo2": "repo2-commit-abc123",
            "repo3": "repo3-commit-abc123",
        })

        config_manager = Mock()
        config = ClaudeIntegrationConfig(dependency_map_enabled=True)
        config_manager.get_claude_integration_config.return_value = config

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=Mock(),
        )

        changed, new, removed = service.detect_changes()

        assert len(changed) == 0
        assert len(new) == 0
        assert len(removed) == 0


class TestAffectedDomainIdentification:
    """Test affected domain identification from _index.md (AC2, AC3, AC4)."""

    def test_identify_affected_domains_for_changed_repo(
        self, dependency_map_service, tmp_golden_repos_root
    ):
        """Test that changed repo maps to correct domains from _index.md."""
        changed_repos = [{"alias": "repo2", "clone_path": str(tmp_golden_repos_root / "repo2")}]
        new_repos = []
        removed_repos = []

        affected = dependency_map_service.identify_affected_domains(
            changed_repos, new_repos, removed_repos
        )

        # repo2 is in both authentication and data-processing domains
        assert "authentication" in affected
        assert "data-processing" in affected
        assert len(affected) == 2

    def test_identify_affected_domains_for_new_repo_not_in_index(
        self, dependency_map_service, tmp_golden_repos_root
    ):
        """Test that new repo not in index triggers domain discovery."""
        changed_repos = []
        new_repos = [{"alias": "repo4", "clone_path": str(tmp_golden_repos_root / "repo4")}]
        removed_repos = []

        affected = dependency_map_service.identify_affected_domains(
            changed_repos, new_repos, removed_repos
        )

        # New repo not in index should trigger __NEW_REPO_DISCOVERY__
        assert "__NEW_REPO_DISCOVERY__" in affected

    def test_identify_affected_domains_for_removed_repo(
        self, dependency_map_service
    ):
        """Test that removed repo maps to its domains for cleanup."""
        changed_repos = []
        new_repos = []
        removed_repos = ["repo3"]

        affected = dependency_map_service.identify_affected_domains(
            changed_repos, new_repos, removed_repos
        )

        # repo3 was in data-processing domain
        assert "data-processing" in affected
