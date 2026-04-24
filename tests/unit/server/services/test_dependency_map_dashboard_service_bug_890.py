"""
Regression tests for Bug #890 — DependencyMapDashboardService metadata filename fix.

Tests that _get_current_commit reads from provider-suffixed metadata-voyage-ai.json
and that None commit (no readable metadata) produces CHANGED, not OK (sentinel tautology fix).
"""

import json
from pathlib import Path
from typing import Optional
from unittest.mock import Mock

import pytest

from code_indexer.server.services.dependency_map_dashboard_service import (
    DependencyMapDashboardService,
)
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


@pytest.fixture
def code_indexer_dir(tmp_path: Path) -> Path:
    """Create .code-indexer directory inside tmp_path."""
    d = tmp_path / ".code-indexer"
    d.mkdir()
    return d


def _make_tracking_backend(commit_hashes_json: Optional[str] = None):
    backend = Mock()
    backend.get_tracking.return_value = {
        "id": 1,
        "status": "completed",
        "last_run": "2024-01-01T00:00:00Z",
        "next_run": None,
        "error_message": None,
        "commit_hashes": commit_hashes_json,
    }
    return backend


def _make_service(commit_hashes_json: Optional[str] = None, dep_map_service=None):
    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
    )
    cm = Mock()
    cm.get_claude_integration_config.return_value = config
    return DependencyMapDashboardService(
        config_manager=cm,
        tracking_backend=_make_tracking_backend(commit_hashes_json),
        dependency_map_service=dep_map_service,
    )


def _compute_status_for_repo(
    tmp_path: Path,
    current_commit: Optional[str],
    stored_commit: Optional[str],
) -> str:
    """Helper: build a single-repo dashboard, return its computed status string."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir(exist_ok=True)
    code_indexer_dir = repo_dir / ".code-indexer"
    code_indexer_dir.mkdir(exist_ok=True)

    if current_commit is not None:
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": current_commit})
        )

    dep_map_svc = Mock()
    dep_map_svc.get_activated_repos.return_value = [
        {"alias": "my-repo", "clone_path": str(repo_dir)}
    ]

    stored_hashes = {"my-repo": stored_commit}
    svc = _make_service(json.dumps(stored_hashes), dep_map_svc)
    statuses = svc._compute_repo_statuses(stored_hashes=stored_hashes, domain_map={})
    return next(s["status"] for s in statuses if s["alias"] == "my-repo")


class TestGetCurrentCommitReadsVoyageFile:
    """_get_current_commit must delegate to read_current_commit (voyage-first)."""

    def test_returns_real_sha_from_voyage_file(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Voyage file present -> real SHA returned (not sentinel 'local')."""
        (code_indexer_dir / "metadata-voyage-ai.json").write_text(
            json.dumps({"current_commit": "realsha123"})
        )
        svc = _make_service()

        result = svc._get_current_commit("my-repo", str(tmp_path))

        assert result == "realsha123"

    def test_returns_none_when_no_metadata_file_exists(
        self, tmp_path: Path, code_indexer_dir: Path
    ) -> None:
        """Neither metadata file present -> None (not 'local' or 'unknown' sentinel)."""
        svc = _make_service()

        result = svc._get_current_commit("my-repo", str(tmp_path))

        assert result is None
        assert result != "local"
        assert result != "unknown"


class TestComputeRepoStatusesNoneHandling:
    """_compute_repo_statuses must not produce OK when current_commit is None."""

    def test_none_current_with_stored_sha_yields_changed(self, tmp_path: Path) -> None:
        """No metadata file -> None commit. Stored hash exists -> CHANGED (not OK)."""
        status = _compute_status_for_repo(
            tmp_path, current_commit=None, stored_commit="stored-sha"
        )
        assert status == "CHANGED"

    def test_matching_real_shas_yields_ok(self, tmp_path: Path) -> None:
        """Current SHA matches stored SHA -> OK (regression guard)."""
        status = _compute_status_for_repo(
            tmp_path, current_commit="abc123", stored_commit="abc123"
        )
        assert status == "OK"

    def test_different_real_shas_yields_changed(self, tmp_path: Path) -> None:
        """Current SHA differs from stored SHA -> CHANGED."""
        status = _compute_status_for_repo(
            tmp_path, current_commit="new-sha", stored_commit="old-sha"
        )
        assert status == "CHANGED"
