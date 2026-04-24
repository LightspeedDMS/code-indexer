"""
Regression tests for Bug #890 — DependencyMapService metadata filename fix.

Tests that _get_commit_hashes reads from provider-suffixed metadata-voyage-ai.json
and never stores "local"/"unknown" sentinels. Also tests that detect_changes correctly
flags repos as CHANGED when SHA differs from stored tracking value.
"""

import json
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import Mock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


@pytest.fixture
def golden_repos_root(tmp_path: Path) -> Path:
    """Create a minimal golden-repos root directory."""
    root = tmp_path / "golden-repos"
    root.mkdir()
    return root


def _make_repo(root: Path, alias: str, commit: Optional[str] = None) -> Path:
    """Create repo directory with optional voyage metadata and a source file."""
    repo_dir = root / alias
    repo_dir.mkdir(exist_ok=True)
    code_indexer_dir = repo_dir / ".code-indexer"
    code_indexer_dir.mkdir(exist_ok=True)
    (repo_dir / "main.py").write_text(f"# {alias}\n")
    if commit is not None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": commit})
        )
    return repo_dir


def _make_service(
    golden_repos_root: Path,
    activated_repos: list,
    stored_hashes: Optional[Dict[str, Optional[str]]] = None,
) -> DependencyMapService:
    """Build a DependencyMapService with mocked dependencies."""
    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
    )
    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = config

    tracking_backend = Mock()
    tracking_backend.get_tracking.return_value = {
        "id": 1,
        "status": "completed",
        "last_run": "2024-01-01T00:00:00Z",
        "next_run": None,
        "error_message": None,
        "commit_hashes": json.dumps(stored_hashes)
        if stored_hashes is not None
        else None,
    }

    golden_repos_manager = Mock()
    golden_repos_manager.golden_repos_dir = golden_repos_root
    golden_repos_manager.list_golden_repos.return_value = activated_repos
    golden_repos_manager.get_actual_repo_path.side_effect = lambda alias: str(
        golden_repos_root / alias
    )

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=Mock(),
    )


class TestGetCommitHashesReadsVoyageFile:
    """_get_commit_hashes must read metadata-voyage-ai.json, not metadata.json."""

    def test_returns_real_sha_from_voyage_file(self, golden_repos_root: Path) -> None:
        """Voyage file present -> real SHA in result dict."""
        _make_repo(golden_repos_root, "repo1", commit="realsha123")
        repo_list = [{"alias": "repo1", "clone_path": str(golden_repos_root / "repo1")}]
        svc = _make_service(golden_repos_root, repo_list)

        result = svc._get_commit_hashes(repo_list)

        assert result.get("repo1") == "realsha123"

    def test_key_absent_when_metadata_missing(self, golden_repos_root: Path) -> None:
        """No metadata file -> key absent from result dict (not 'local' or 'unknown')."""
        _make_repo(golden_repos_root, "repo1", commit=None)
        repo_list = [{"alias": "repo1", "clone_path": str(golden_repos_root / "repo1")}]
        svc = _make_service(golden_repos_root, repo_list)

        result = svc._get_commit_hashes(repo_list)

        assert "repo1" not in result


class TestDetectChangesUsesVoyageMetadata:
    """detect_changes must flag repos CHANGED when voyage SHA differs from stored."""

    def test_flags_changed_when_voyage_sha_differs_from_stored(
        self, golden_repos_root: Path
    ) -> None:
        """Current voyage SHA != stored SHA -> repo in changed list."""
        _make_repo(golden_repos_root, "repo1", commit="new-sha")
        activated = [{"alias": "repo1", "clone_path": str(golden_repos_root / "repo1")}]
        svc = _make_service(
            golden_repos_root, activated, stored_hashes={"repo1": "old-sha"}
        )

        changed, new, removed = svc.detect_changes()

        assert any(r["alias"] == "repo1" for r in changed)

    def test_new_when_both_stored_and_metadata_absent(
        self, golden_repos_root: Path
    ) -> None:
        """No metadata and no stored entry -> repo in new list, not changed."""
        _make_repo(golden_repos_root, "repo1", commit=None)
        activated = [{"alias": "repo1", "clone_path": str(golden_repos_root / "repo1")}]
        # No stored hashes at all (first run)
        svc = _make_service(golden_repos_root, activated, stored_hashes=None)

        changed, new, removed = svc.detect_changes()

        assert any(r["alias"] == "repo1" for r in new)
        assert not any(r["alias"] == "repo1" for r in changed)

    def test_not_changed_when_voyage_sha_matches_stored(
        self, golden_repos_root: Path
    ) -> None:
        """Current voyage SHA == stored SHA -> repo NOT in changed list."""
        _make_repo(golden_repos_root, "repo1", commit="same-sha")
        activated = [{"alias": "repo1", "clone_path": str(golden_repos_root / "repo1")}]
        svc = _make_service(
            golden_repos_root, activated, stored_hashes={"repo1": "same-sha"}
        )

        changed, new, removed = svc.detect_changes()

        assert not any(r["alias"] == "repo1" for r in changed)

    def test_stored_local_sentinel_plus_real_sha_yields_changed(
        self, golden_repos_root: Path
    ) -> None:
        """Stored sentinel 'local' != real voyage SHA -> repo is CHANGED."""
        _make_repo(golden_repos_root, "repo1", commit="realsha456")
        activated = [{"alias": "repo1", "clone_path": str(golden_repos_root / "repo1")}]
        svc = _make_service(
            golden_repos_root,
            activated,
            stored_hashes={"repo1": "local"},
        )

        changed, new, removed = svc.detect_changes()

        assert any(r["alias"] == "repo1" for r in changed)
