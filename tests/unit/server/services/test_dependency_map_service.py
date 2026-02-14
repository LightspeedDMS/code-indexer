"""
Unit tests for DependencyMapService (Story #192, Component 3).

Tests the orchestration service for dependency map analysis pipeline.
Uses real filesystem for stage-then-swap testing (anti-mock methodology).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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
    for alias in ["repo1", "repo2"]:
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

    # Create sample repo description files in cidx-meta
    (cidx_meta / "repo1.md").write_text("# Repo 1\n\nDescription of repo 1")
    (cidx_meta / "repo2.md").write_text("# Repo 2\n\nDescription of repo 2")

    return golden_repos_root


@pytest.fixture
def mock_config_manager():
    """Create mock config manager with real ClaudeIntegrationConfig."""
    config_manager = Mock()
    # Use real ClaudeIntegrationConfig to catch attribute name mismatches
    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
        dependency_map_pass1_max_turns=5,
        dependency_map_pass2_max_turns=10,
        dependency_map_pass3_max_turns=5,
    )
    config_manager.get_claude_integration_config.return_value = config
    return config_manager


@pytest.fixture
def mock_tracking_backend():
    """Create mock tracking backend."""
    backend = Mock()
    backend.get_tracking.return_value = {
        "id": 1,
        "last_run": None,
        "next_run": None,
        "status": "pending",
        "commit_hashes": None,
        "error_message": None,
    }
    return backend


@pytest.fixture
def mock_analyzer():
    """Create mock analyzer."""
    analyzer = Mock()

    # Mock Pass 1 to return domain list
    analyzer.run_pass_1_synthesis.return_value = [
        {"name": "domain1", "description": "Domain 1", "participating_repos": ["repo1"]},
        {"name": "domain2", "description": "Domain 2", "participating_repos": ["repo2"]},
    ]

    return analyzer


@pytest.fixture
def mock_golden_repos_manager(tmp_golden_repos_root: Path):
    """Create mock golden repos manager."""
    manager = Mock()
    manager.golden_repos_dir = tmp_golden_repos_root
    manager.list_golden_repos.return_value = [
        {
            "alias": "repo1",
            "clone_path": str(tmp_golden_repos_root / "repo1"),
            "repo_url": "git@github.com:org/repo1.git",
        },
        {
            "alias": "repo2",
            "clone_path": str(tmp_golden_repos_root / "repo2"),
            "repo_url": "git@github.com:org/repo2.git",
        },
    ]
    return manager


def test_run_full_analysis_disabled_returns_early(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis returns early if disabled in config."""
    # Disable dependency map
    mock_config_manager.get_claude_integration_config.return_value.dependency_map_enabled = False

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    result = service.run_full_analysis()

    assert result["status"] == "disabled"
    assert "disabled" in result["message"].lower()
    mock_analyzer.run_pass_1_synthesis.assert_not_called()


def test_run_full_analysis_no_repos_returns_skipped(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis skips if no golden repos exist."""
    # Return empty repo list
    mock_golden_repos_manager.list_golden_repos.return_value = []

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    result = service.run_full_analysis()

    assert result["status"] == "skipped"
    assert "no" in result["message"].lower()
    mock_analyzer.run_pass_1_synthesis.assert_not_called()


def test_run_full_analysis_concurrency_protection(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis raises if already running (lock protection)."""
    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    # Acquire lock manually
    service._lock.acquire()

    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            service.run_full_analysis()
    finally:
        service._lock.release()


def test_run_full_analysis_stage_then_swap_with_real_filesystem(
    tmp_golden_repos_root: Path,
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis performs stage-then-swap atomically on real filesystem."""
    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    cidx_meta = tmp_golden_repos_root / "cidx-meta"
    final_dir = cidx_meta / "dependency-map"
    staging_dir = cidx_meta / "dependency-map.staging"

    # Create existing final_dir with old content
    final_dir.mkdir()
    (final_dir / "old-file.md").write_text("old content")

    # Mock subprocess.run for cidx index
    with patch("subprocess.run") as mock_run:
        result = service.run_full_analysis()

    assert result["status"] == "completed"
    assert result["domains_count"] == 2
    assert result["repos_analyzed"] == 2

    # Verify staging dir was removed after swap
    assert not staging_dir.exists()

    # Verify final dir exists
    assert final_dir.exists()

    # Verify old content was replaced (old-file.md should not exist)
    assert not (final_dir / "old-file.md").exists()


def test_run_full_analysis_updates_tracking_to_running(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis updates tracking status to running before analysis."""
    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    with patch("subprocess.run"):
        service.run_full_analysis()

    # Verify tracking was updated to running
    calls = mock_tracking_backend.update_tracking.call_args_list
    # First call should set status=running
    assert any(
        call.kwargs.get("status") == "running" for call in calls
    ), f"Expected status='running' in calls: {calls}"


def test_run_full_analysis_updates_tracking_to_completed_with_commit_hashes(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis updates tracking to completed with commit hashes."""
    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    with patch("subprocess.run"):
        result = service.run_full_analysis()

    assert result["status"] == "completed"

    # Verify tracking was updated to completed with commit hashes
    calls = mock_tracking_backend.update_tracking.call_args_list
    completed_call = next(
        (call for call in calls if call.kwargs.get("status") == "completed"), None
    )
    assert completed_call is not None, f"Expected status='completed' in calls: {calls}"

    # Verify commit_hashes is a JSON string
    commit_hashes_str = completed_call.kwargs.get("commit_hashes")
    assert commit_hashes_str is not None
    commit_hashes = json.loads(commit_hashes_str)
    assert "repo1" in commit_hashes
    assert "repo2" in commit_hashes
    assert commit_hashes["repo1"] == "repo1-commit-abc123"


def test_run_full_analysis_updates_tracking_to_failed_on_error(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis updates tracking to failed if analysis fails."""
    # Make Pass 1 raise an exception
    mock_analyzer.run_pass_1_synthesis.side_effect = Exception("Claude CLI timeout")

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    with pytest.raises(Exception, match="Claude CLI timeout"):
        service.run_full_analysis()

    # Verify tracking was updated to failed
    mock_tracking_backend.update_tracking.assert_called_with(
        status="failed", error_message="Claude CLI timeout"
    )


def test_run_full_analysis_pass_2_continues_on_domain_failure(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis continues Pass 2 if one domain fails."""
    # Make Pass 2 fail for first domain but succeed for second
    call_count = 0

    def pass_2_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Domain 1 analysis failed")
        # Second call succeeds (no exception)

    mock_analyzer.run_pass_2_per_domain.side_effect = pass_2_side_effect

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    with patch("subprocess.run"):
        result = service.run_full_analysis()

    # Should complete with errors
    assert result["status"] == "completed"
    assert len(result["errors"]) == 1
    assert "domain1" in result["errors"][0]
    assert "Domain 1 analysis failed" in result["errors"][0]

    # Verify Pass 2 was called twice (both domains attempted)
    assert mock_analyzer.run_pass_2_per_domain.call_count == 2


def test_run_full_analysis_invokes_cidx_index_reindex(
    tmp_golden_repos_root: Path,
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis invokes cidx index to re-index cidx-meta."""
    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    cidx_meta = tmp_golden_repos_root / "cidx-meta"

    with patch("subprocess.run") as mock_run:
        service.run_full_analysis()

    # Verify subprocess.run was called with cidx index
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    assert call_args[0][0] == ["cidx", "index"]
    assert call_args[1]["cwd"] == str(cidx_meta)
    assert call_args[1]["timeout"] == 120


def test_run_full_analysis_cleans_up_claude_md(
    tmp_golden_repos_root: Path,
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test run_full_analysis cleans up CLAUDE.md after completion."""
    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    claude_md = tmp_golden_repos_root / "CLAUDE.md"

    with patch("subprocess.run"):
        service.run_full_analysis()

    # Verify CLAUDE.md was cleaned up
    assert not claude_md.exists()


def test_get_activated_repos_skips_markdown_headings(
    tmp_golden_repos_root: Path,
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test _get_activated_repos skips markdown headings when extracting description."""
    cidx_meta = tmp_golden_repos_root / "cidx-meta"

    # Test Case 1: Markdown file with frontmatter + heading + actual description
    (cidx_meta / "repo1.md").write_text(
        "---\ntitle: Repo 1\n---\n# claude-usage\nActual description of repo 1"
    )

    # Test Case 2: Markdown file with only headings (should fall back to "No description")
    (cidx_meta / "repo2.md").write_text("---\ntitle: Repo 2\n---\n# Heading\n## Subheading")

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    repos = service._get_activated_repos()

    # Verify repo1 extracted actual description, not heading
    repo1 = next((r for r in repos if r["alias"] == "repo1"), None)
    assert repo1 is not None
    assert repo1["description_summary"] == "Actual description of repo 1"

    # Verify repo2 falls back to "No description" when only headings exist
    repo2 = next((r for r in repos if r["alias"] == "repo2"), None)
    assert repo2 is not None
    assert repo2["description_summary"] == "No description"
