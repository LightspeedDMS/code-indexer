"""
Unit tests for Bug #1623: stale-index status check blind to provider-
suffixed metadata files.

RefreshScheduler._check_stale_index_metadata()'s in_progress/failed
interrupted-index signal (Bug #1508) only ever read the legacy bare
metadata.json for the `status` field. Bug #1591 already made the sibling
current_commit signal provider-aware (via metadata_reader.read_current_commit()),
but status was left reading the bare metadata.json directly -- the last
remaining part of this check blind to provider-suffixed files
(metadata-voyage-ai.json, metadata-cohere.json).

Live evidence (real fleet): colorama/metadata-voyage-ai.json and
markupsafe/metadata-voyage-ai.json both record status=in_progress while
their sibling metadata-cohere.json files say completed -- exactly the
interrupted-index condition Bug #1508 exists to catch, sitting in a file
the status check never opened.

These tests exercise the REAL `_check_stale_index_metadata()` method
against a REAL local git repository (mirrors
test_refresh_scheduler_stale_index_prefix_sha_1591.py's pattern) -- no
mocking of git itself.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager

REFRESH_INTERVAL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_git_repo():
    """Create a real, temporary git repository with one commit."""
    repo_dir = Path(tempfile.mkdtemp(prefix="test_stale_index_status_1623_"))
    try:
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        (repo_dir / "file1.txt").write_text("content1")
        subprocess.run(
            ["git", "add", "."], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        yield repo_dir
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def _actual_head(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_metadata(source_path: Path, filename: str, **fields):
    meta_dir = source_path / ".code-indexer"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with open(meta_dir / filename, "w") as f:
        json.dump(fields, f)


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = REFRESH_INTERVAL_SECONDS
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
        "default_branch": "main",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# The blocking gap: an in_progress/failed status recorded ONLY in a
# provider-suffixed file must be detected, reproducing the real
# colorama/markupsafe fleet scenario (provider file in_progress, legacy
# file absent or disagreeing).
# ---------------------------------------------------------------------------


class TestProviderSuffixedStatusDetected:
    def test_in_progress_status_in_voyage_file_only_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        """Reproduces the exact colorama/markupsafe scenario: ONLY
        metadata-voyage-ai.json exists, recording status=in_progress --
        before the fix, this file was never opened for the status field
        and the interrupted index was silently invisible."""
        actual_head = _actual_head(real_git_repo)
        _write_metadata(
            real_git_repo,
            "metadata-voyage-ai.json",
            status="in_progress",
            current_commit=actual_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "colorama-global"
        )

        assert result is True, (
            "A status=in_progress recorded ONLY in the provider-suffixed "
            "metadata file (the real production filename) must force a "
            "reconcile -- this is the exact live colorama/markupsafe "
            "fleet scenario Bug #1623 exists to fix."
        )

    def test_failed_status_in_voyage_file_only_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        _write_metadata(
            real_git_repo,
            "metadata-voyage-ai.json",
            status="failed",
            current_commit=actual_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "markupsafe-global"
        )

        assert result is True

    def test_provider_status_takes_precedence_over_stale_legacy_status(
        self, scheduler, real_git_repo
    ):
        """Precedence guard matching read_status()'s documented contract:
        the provider file wins even when the legacy file disagrees --
        mirrors the real colorama scenario where metadata-cohere.json (a
        DIFFERENT provider file, not consulted by read_status()) says
        completed while metadata-voyage-ai.json says in_progress. Here we
        exercise the read_status()-covered pair directly: legacy says
        completed, voyage (the file read_status() actually prefers) says
        in_progress -- the provider file must win."""
        actual_head = _actual_head(real_git_repo)
        _write_metadata(
            real_git_repo,
            "metadata.json",
            status="completed",
            current_commit=actual_head,
        )
        _write_metadata(
            real_git_repo,
            "metadata-voyage-ai.json",
            status="in_progress",
            current_commit=actual_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is True, (
            "The provider-suffixed file's in_progress status must take "
            "precedence over a stale-but-unused legacy file claiming "
            "completed."
        )


# ---------------------------------------------------------------------------
# Regression guard: fully consistent provider-suffixed metadata (status
# completed, current_commit matching HEAD) must still skip reconcile.
# ---------------------------------------------------------------------------


class TestConsistentProviderMetadataStillSkips:
    def test_completed_status_in_voyage_file_does_not_force_reconcile(
        self, scheduler, real_git_repo
    ):
        actual_head = _actual_head(real_git_repo)
        _write_metadata(
            real_git_repo,
            "metadata-voyage-ai.json",
            status="completed",
            current_commit=actual_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is False, (
            "Consistent, completed status in the provider-suffixed file "
            "must not force a reconcile pass."
        )

    def test_legacy_only_in_progress_status_still_forces_reconcile(
        self, scheduler, real_git_repo
    ):
        """Regression guard: the original Bug #1508 legacy-file behavior
        must continue to work when no provider file exists at all."""
        actual_head = _actual_head(real_git_repo)
        _write_metadata(
            real_git_repo,
            "metadata.json",
            status="in_progress",
            current_commit=actual_head,
        )

        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is True

    def test_no_metadata_files_does_not_force_reconcile(self, scheduler, real_git_repo):
        result = scheduler._check_stale_index_metadata(
            str(real_git_repo), "some-repo-global"
        )

        assert result is False
