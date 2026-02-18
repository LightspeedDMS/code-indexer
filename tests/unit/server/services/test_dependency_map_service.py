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


def test_read_repo_descriptions_filters_stale_repos(
    tmp_golden_repos_root: Path,
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
):
    """Test Fix 8: _read_repo_descriptions filters out stale repos not in active set."""
    cidx_meta = tmp_golden_repos_root / "cidx-meta"

    # Create descriptions for 3 repos
    (cidx_meta / "repo1.md").write_text("# Repo 1\nActive repo 1")
    (cidx_meta / "repo2.md").write_text("# Repo 2\nActive repo 2")
    (cidx_meta / "stale-repo.md").write_text("# Stale Repo\nThis repo is no longer registered")

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
    )

    # Call with active_aliases containing only repo1 and repo2
    active_aliases = {"repo1", "repo2"}
    descriptions = service._read_repo_descriptions(cidx_meta, active_aliases=active_aliases)

    # Verify only active repos are included
    assert "repo1" in descriptions
    assert "repo2" in descriptions
    assert "stale-repo" not in descriptions
    assert len(descriptions) == 2


class TestIteration15Journal:
    """Test Iteration 15: Journal-based resumability for dependency map pipeline."""

    def test_journal_created_on_fresh_run(
        self,
        tmp_golden_repos_root: Path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that _journal.json is created in staging_dir during fresh run."""
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        with patch("subprocess.run"):
            service.run_full_analysis()

        # Verify journal exists in final directory after swap
        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        dep_map_dir = cidx_meta / "dependency-map"
        journal_path = dep_map_dir / "_journal.json"
        assert journal_path.exists(), "Journal should exist in final directory after swap"
        journal = json.loads(journal_path.read_text())
        assert journal["pass1"]["status"] == "completed"
        assert journal["pass3"]["status"] == "completed"
        assert "repo_sizes" in journal

    def test_journal_skip_completed_pass1(
        self,
        tmp_golden_repos_root: Path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that Pass 1 is skipped when journal shows it's completed."""
        # Create files in repos to match journal sizes (so _should_resume doesn't reject it)
        repo1_dir = tmp_golden_repos_root / "repo1"
        repo2_dir = tmp_golden_repos_root / "repo2"
        (repo1_dir / "file1.py").write_text("x" * 100000)  # 100KB
        (repo2_dir / "file1.py").write_text("x" * 50000)   # 50KB

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)

        # Create a journal with completed Pass 1 (sizes must match actual repo sizes within 5%)
        journal = {
            "pipeline_id": "test-123",
            "started_at": "2026-02-15T12:00:00Z",
            "repo_sizes": {
                "repo1": {"file_count": 2, "total_bytes": 100000},  # file1.py + metadata.json
                "repo2": {"file_count": 2, "total_bytes": 50000},
            },
            "pass1": {"status": "completed", "domains_count": 2},
            "pass2": {},
            "pass3": {"status": "pending"},
        }
        (staging_dir / "_journal.json").write_text(json.dumps(journal))

        # Create _domains.json that should be loaded instead of running Pass 1
        domains = [
            {"name": "domain1", "description": "Domain 1", "participating_repos": ["repo1"]},
            {"name": "domain2", "description": "Domain 2", "participating_repos": ["repo2"]},
        ]
        (staging_dir / "_domains.json").write_text(json.dumps(domains))

        with patch("subprocess.run"):
            service.run_full_analysis()

        # Verify Pass 1 was NOT called (analyzer.run_pass_1_synthesis should not be called)
        assert mock_analyzer.run_pass_1_synthesis.call_count == 0

    def test_journal_skip_completed_domain(
        self,
        tmp_golden_repos_root: Path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that completed domains in Pass 2 are skipped."""
        # Create files in repos to match journal sizes
        repo1_dir = tmp_golden_repos_root / "repo1"
        repo2_dir = tmp_golden_repos_root / "repo2"
        (repo1_dir / "file1.py").write_text("x" * 100000)
        (repo2_dir / "file1.py").write_text("x" * 50000)

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)

        # Create journal with domain1 completed, domain2 pending
        journal = {
            "pipeline_id": "test-456",
            "started_at": "2026-02-15T12:00:00Z",
            "repo_sizes": {
                "repo1": {"file_count": 2, "total_bytes": 100000},
                "repo2": {"file_count": 2, "total_bytes": 50000},
            },
            "pass1": {"status": "completed", "domains_count": 2},
            "pass2": {
                "domain1": {"status": "completed", "chars": 5000},
                "domain2": {"status": "pending"},
            },
            "pass3": {"status": "pending"},
        }
        (staging_dir / "_journal.json").write_text(json.dumps(journal))

        # Create domains list
        domains = [
            {"name": "domain1", "description": "Domain 1", "participating_repos": ["repo1"]},
            {"name": "domain2", "description": "Domain 2", "participating_repos": ["repo2"]},
        ]
        (staging_dir / "_domains.json").write_text(json.dumps(domains))

        # Mock analyzer to track which domains are analyzed
        mock_analyzer.run_pass_2_per_domain = Mock()

        with patch("subprocess.run"):
            service.run_full_analysis()

        # Verify run_pass_2_per_domain was called only for domain2 (not domain1)
        # Should be called once (for domain2 only)
        assert mock_analyzer.run_pass_2_per_domain.call_count == 1

    def test_journal_fresh_start_on_repo_change(
        self,
        tmp_golden_repos_root: Path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that journal is discarded and fresh start happens when repo size changes >5%."""
        # Create repo with different size than journal
        repo1_dir = tmp_golden_repos_root / "repo1"
        (repo1_dir / "newfile.py").write_text("x" * 10000)  # Add new content

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)

        # Create journal with OLD repo sizes (significantly different from current)
        journal = {
            "pipeline_id": "old-789",
            "started_at": "2026-02-14T12:00:00Z",
            "repo_sizes": {
                "repo1": {"file_count": 10, "total_bytes": 100000},  # Much smaller than current
                "repo2": {"file_count": 50, "total_bytes": 500000},
            },
            "pass1": {"status": "completed", "domains_count": 2},
            "pass2": {},
            "pass3": {"status": "pending"},
        }
        (staging_dir / "_journal.json").write_text(json.dumps(journal))

        with patch("subprocess.run"):
            service.run_full_analysis()

        # Verify Pass 1 WAS called (fresh start, journal discarded)
        assert mock_analyzer.run_pass_1_synthesis.call_count == 1

    def test_journal_fresh_start_on_new_repo(
        self,
        tmp_golden_repos_root: Path,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that journal is discarded when a new repo is added (not in journal)."""
        # Create mock that returns 3 repos (repo1, repo2, repo3)
        mock_golden_repos_manager = Mock()
        mock_golden_repos_manager.golden_repos_dir = str(tmp_golden_repos_root)
        mock_golden_repos_manager.list_golden_repos.return_value = [
            {"alias": "repo1", "clone_path": str(tmp_golden_repos_root / "repo1")},
            {"alias": "repo2", "clone_path": str(tmp_golden_repos_root / "repo2")},
            {"alias": "repo3", "clone_path": str(tmp_golden_repos_root / "repo3")},  # NEW
        ]

        # Create repo3 directory
        repo3_dir = tmp_golden_repos_root / "repo3"
        repo3_dir.mkdir()
        (repo3_dir / ".code-indexer").mkdir()
        (repo3_dir / ".code-indexer" / "metadata.json").write_text(
            json.dumps({"current_commit": "repo3-commit-xyz"})
        )

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"
        staging_dir.mkdir(parents=True)

        # Create journal with ONLY repo1 and repo2 (repo3 is new)
        journal = {
            "pipeline_id": "old-999",
            "started_at": "2026-02-14T12:00:00Z",
            "repo_sizes": {
                "repo1": {"file_count": 100, "total_bytes": 1000000},
                "repo2": {"file_count": 50, "total_bytes": 500000},
                # repo3 NOT in journal
            },
            "pass1": {"status": "completed", "domains_count": 2},
            "pass2": {},
            "pass3": {"status": "pending"},
        }
        (staging_dir / "_journal.json").write_text(json.dumps(journal))

        with patch("subprocess.run"):
            service.run_full_analysis()

        # Verify Pass 1 WAS called (fresh start due to new repo)
        assert mock_analyzer.run_pass_1_synthesis.call_count == 1

    def test_journal_saved_after_each_domain(
        self,
        tmp_golden_repos_root: Path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that journal is saved incrementally after each domain completes."""
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        cidx_meta = tmp_golden_repos_root / "cidx-meta"
        staging_dir = cidx_meta / "dependency-map.staging"

        # Track journal writes by monitoring file writes
        journal_writes = []

        original_write_text = Path.write_text

        def track_journal_writes(self, content, *args, **kwargs):
            # Track writes to both _journal.tmp (atomic write) and _journal.json (legacy/test direct writes)
            if self.name in ("_journal.json", "_journal.tmp"):
                journal_writes.append(json.loads(content))
            return original_write_text(self, content, *args, **kwargs)

        with patch.object(Path, "write_text", track_journal_writes):
            with patch("subprocess.run"):
                service.run_full_analysis()

        # Verify journal was written multiple times (after Pass 1, after each domain, after Pass 3)
        # Should be at least 4 writes: initial, after pass1, after 2 domains, after pass3
        assert len(journal_writes) >= 4

    def test_enrich_repo_sizes_sorts_descending(
        self,
        tmp_golden_repos_root: Path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """Test that _enrich_repo_sizes computes sizes and sorts repos by total_bytes descending."""
        # Create repos with different sizes
        small_repo = tmp_golden_repos_root / "small-repo"
        small_repo.mkdir()
        (small_repo / "file1.txt").write_text("x" * 100)

        large_repo = tmp_golden_repos_root / "large-repo"
        large_repo.mkdir()
        (large_repo / "file1.txt").write_text("x" * 10000)
        (large_repo / "file2.txt").write_text("x" * 10000)

        medium_repo = tmp_golden_repos_root / "medium-repo"
        medium_repo.mkdir()
        (medium_repo / "file1.txt").write_text("x" * 5000)

        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        repo_list = [
            {"alias": "small-repo", "clone_path": str(small_repo)},
            {"alias": "large-repo", "clone_path": str(large_repo)},
            {"alias": "medium-repo", "clone_path": str(medium_repo)},
        ]

        # Call _enrich_repo_sizes
        enriched = service._enrich_repo_sizes(repo_list)

        # Verify sizes were computed
        assert all("file_count" in r for r in enriched)
        assert all("total_bytes" in r for r in enriched)

        # Verify sorted descending by total_bytes
        assert enriched[0]["alias"] == "large-repo"
        assert enriched[1]["alias"] == "medium-repo"
        assert enriched[2]["alias"] == "small-repo"

        # Verify file counts are correct
        assert enriched[0]["file_count"] == 2  # large has 2 files
        assert enriched[1]["file_count"] == 1  # medium has 1 file
        assert enriched[2]["file_count"] == 1  # small has 1 file


# ─────────────────────────────────────────────────────────────────────────────
# Story #217 AC9: _record_run_metrics edge counting with new heading
# ─────────────────────────────────────────────────────────────────────────────


class TestRecordRunMetricsAC217:
    """AC9 (Story #217): _record_run_metrics() edge counting works with new heading.

    The new _build_cross_domain_graph() writes the heading as
    '## Cross-Domain Dependency Graph' (not 'Cross-Domain Dependencies').
    _record_run_metrics() must detect BOTH variants so that edge_count is
    correct for both old and new _index.md files.
    """

    def _build_service(self, mock_golden_repos_manager, mock_config_manager, mock_tracking_backend, mock_analyzer):
        return DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

    def test_record_run_metrics_counts_edges_with_old_heading(
        self,
        tmp_path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """AC9: Old heading 'Cross-Domain Dependencies' is still counted correctly."""
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()

        # Old-format _index.md with old heading
        (output_dir / "_index.md").write_text(
            "# Dependency Map Index\n\n"
            "## Cross-Domain Dependencies\n\n"
            "| Source Domain | Target Domain | Via Repos |\n"
            "|---|---|---|\n"
            "| domain-a | domain-b | repo-a |\n"
            "| domain-b | domain-c | repo-b |\n\n"
            "## Another Section\n"
        )
        # Create domain files so total_chars is computed
        domain_list = [{"name": "domain-a"}, {"name": "domain-b"}]
        for d in domain_list:
            (output_dir / f"{d['name']}.md").write_text("some content")

        repo_list = [{"alias": "repo-a"}, {"alias": "repo-b"}]

        service = self._build_service(
            mock_golden_repos_manager, mock_config_manager, mock_tracking_backend, mock_analyzer
        )
        service._record_run_metrics(output_dir, domain_list, repo_list)

        # Verify record_run_metrics was called with edge_count=2
        mock_tracking_backend.record_run_metrics.assert_called_once()
        recorded = mock_tracking_backend.record_run_metrics.call_args[0][0]
        assert recorded["edge_count"] == 2, (
            f"Expected edge_count=2 with old heading, got {recorded['edge_count']}"
        )

    def test_record_run_metrics_counts_edges_with_new_heading(
        self,
        tmp_path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """AC9: New heading '## Cross-Domain Dependency Graph' is detected and counted.

        This test will FAIL until line 463 of dependency_map_service.py is updated
        to check for BOTH heading variants.
        """
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()

        # New-format _index.md with new heading (5-column table)
        (output_dir / "_index.md").write_text(
            "---\nschema_version: 1.0\n---\n\n"
            "## Cross-Domain Dependency Graph\n\n"
            "| Source Domain | Target Domain | Via Repos | Type | Why |\n"
            "|---|---|---|---|---|\n"
            "| domain-a | domain-b | repo-a | Service integration | domain-a calls domain-b API |\n"
            "| domain-b | domain-c | repo-b | Code-level | shared library |\n\n"
            "## Another Section\n"
        )
        domain_list = [{"name": "domain-a"}, {"name": "domain-b"}]
        for d in domain_list:
            (output_dir / f"{d['name']}.md").write_text("some content")

        repo_list = [{"alias": "repo-a"}, {"alias": "repo-b"}]

        service = self._build_service(
            mock_golden_repos_manager, mock_config_manager, mock_tracking_backend, mock_analyzer
        )
        service._record_run_metrics(output_dir, domain_list, repo_list)

        # Verify record_run_metrics was called with edge_count=2
        mock_tracking_backend.record_run_metrics.assert_called_once()
        recorded = mock_tracking_backend.record_run_metrics.call_args[0][0]
        assert recorded["edge_count"] == 2, (
            f"Expected edge_count=2 with new '## Cross-Domain Dependency Graph' heading, "
            f"got {recorded['edge_count']}. "
            f"Fix: line 463 of dependency_map_service.py must check for BOTH headings."
        )

    def test_record_run_metrics_edge_count_zero_no_index_file(
        self,
        tmp_path,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """AC9: If _index.md does not exist, edge_count is 0 (no crash)."""
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()
        # No _index.md created

        domain_list = [{"name": "domain-a"}]
        (output_dir / "domain-a.md").write_text("some content")
        repo_list = [{"alias": "repo-a"}]

        service = self._build_service(
            mock_golden_repos_manager, mock_config_manager, mock_tracking_backend, mock_analyzer
        )
        service._record_run_metrics(output_dir, domain_list, repo_list)

        mock_tracking_backend.record_run_metrics.assert_called_once()
        recorded = mock_tracking_backend.record_run_metrics.call_args[0][0]
        assert recorded["edge_count"] == 0
