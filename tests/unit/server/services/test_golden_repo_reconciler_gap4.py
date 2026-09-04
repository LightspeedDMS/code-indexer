"""
Tests for GAP-4: index staleness detection in the reconcile sweep.

For each healthy golden repo that survived Pass 1 classification, compares the
git HEAD of the on-disk clone against the ``current_commit`` SHA stored in the
index metadata. If they diverge, triggers a refresh via
``RefreshScheduler.trigger_refresh_for_repo(alias)``.

Uses real git repos (``git init`` + ``git commit``) and real metadata files
(``.code-indexer/metadata-voyage-ai.json``) so the staleness detection
exercises the actual code path, including prefix-tolerant SHA comparison
(Bug #1591) and the ``"unknown"`` literal (always stale).
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepo,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.golden_repo_reconciler import (
    reconcile_golden_repo_registry,
    _get_clone_git_head,
    _index_commit_matches_head,
)
from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: str) -> str:
    """Create a bare-minimum git repo at *path* and return the HEAD SHA."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    # Initial commit so HEAD exists
    marker = os.path.join(path, ".gitkeep")
    with open(marker, "w") as f:
        f.write("")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _add_commit(path: str) -> str:
    """Add a new commit and return its SHA."""
    marker = os.path.join(path, "change.txt")
    with open(marker, "a") as f:
        f.write("change\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_metadata(clone_path: str, current_commit: str) -> None:
    """Write a minimal ``.code-indexer/metadata-voyage-ai.json``."""
    meta_dir = os.path.join(clone_path, ".code-indexer")
    os.makedirs(meta_dir, exist_ok=True)
    meta_path = os.path.join(meta_dir, "metadata-voyage-ai.json")
    with open(meta_path, "w") as f:
        json.dump({"current_commit": current_commit}, f)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestGoldenRepoReconcilerGAP4:
    """End-to-end GAP-4: staleness detection triggers refresh for stale repos."""

    @pytest.fixture
    def temp_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def manager(self, temp_data_dir):
        mgr = GoldenRepoManager(data_dir=temp_data_dir)

        from code_indexer.server.storage.database_manager import DatabaseSchema

        DatabaseSchema(mgr.db_path).initialize_database()

        captured_funcs = []
        mock_bjm = MagicMock(spec=BackgroundJobManager)

        def _capture_and_run(**kwargs):
            captured_funcs.append(kwargs["func"])
            return f"job-{kwargs.get('repo_alias', 'unknown')}"

        mock_bjm.submit_job.side_effect = _capture_and_run
        mgr.background_job_manager = mock_bjm
        mgr._captured_funcs = captured_funcs  # type: ignore[attr-defined]
        return mgr

    @pytest.fixture
    def mock_refresh_scheduler(self):
        scheduler = MagicMock()
        scheduler.trigger_refresh_for_repo.return_value = "job-123"
        return scheduler

    def _register_repo(
        self,
        manager: GoldenRepoManager,
        alias: str,
        *,
        create_clone: bool = True,
        globally_active: bool = True,
    ) -> str:
        """Register a repo and optionally init a real git clone. Returns clone_path.

        When *globally_active* is True (the default), also registers the repo
        in the global registry so ``is_globally_active(alias)`` returns True --
        required for Pass 4 staleness detection to consider the repo.
        """
        clone_path = os.path.join(manager.golden_repos_dir, alias)
        if create_clone:
            _init_git_repo(clone_path)
        repo = GoldenRepo(
            alias=alias,
            repo_url=f"https://github.com/test/{alias}.git",
            default_branch="main",
            clone_path=clone_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            enable_temporal=False,
            temporal_options=None,
        )
        manager.golden_repos[alias] = repo
        manager._sqlite_backend.add_repo(
            alias=repo.alias,
            repo_url=repo.repo_url,
            default_branch=repo.default_branch,
            clone_path=repo.clone_path,
            created_at=repo.created_at,
            enable_temporal=repo.enable_temporal,
            temporal_options=repo.temporal_options,
        )
        if globally_active:
            self._make_globally_active(manager, alias, clone_path)
        return clone_path

    def _make_globally_active(
        self, manager: GoldenRepoManager, alias: str, index_path: str
    ) -> None:
        """Register a ``{alias}-global`` row so ``is_globally_active()`` returns True."""
        golden_repo = manager.golden_repos[alias]
        GlobalReposSqliteBackend(manager.db_path).register_repo(
            alias_name=f"{alias}-global",
            repo_name=alias,
            repo_url=golden_repo.repo_url,
            index_path=index_path,
        )

    def test_stale_index_triggers_refresh(self, manager, mock_refresh_scheduler):
        """A repo whose metadata commit is behind git HEAD triggers a refresh."""
        clone_path = self._register_repo(manager, "repo-stale")
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Write metadata with the current SHA, then advance HEAD
        _write_metadata(clone_path, old_sha)
        _add_commit(clone_path)

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert "repo-stale" in result.stale_indexes_found
        assert "repo-stale" in result.stale_indexes_refreshed
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_called_once_with(
            "repo-stale", submitter_username="system-reconcile"
        )

    def test_current_index_skipped(self, manager, mock_refresh_scheduler):
        """A repo whose metadata commit matches HEAD is NOT flagged stale."""
        clone_path = self._register_repo(manager, "repo-current")
        current_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _write_metadata(clone_path, current_sha)

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert result.stale_indexes_found == []
        assert result.stale_indexes_refreshed == []
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_no_metadata_skipped(self, manager, mock_refresh_scheduler):
        """A repo with no metadata file is silently skipped (never indexed)."""
        self._register_repo(manager, "repo-no-meta")

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert result.stale_indexes_found == []
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_no_scheduler_skips_pass4(self, manager):
        """When no refresh_scheduler is provided, Pass 4 is entirely skipped."""
        clone_path = self._register_repo(manager, "repo-stale")
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _write_metadata(clone_path, old_sha)
        _add_commit(clone_path)

        result = reconcile_golden_repo_registry(manager)

        assert result.stale_indexes_found == []
        assert result.stale_indexes_refreshed == []

    def test_refresh_failure_recorded(self, manager, mock_refresh_scheduler):
        """When trigger_refresh_for_repo raises, the alias lands in stale_indexes_failed."""
        clone_path = self._register_repo(manager, "repo-fail")
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _write_metadata(clone_path, old_sha)
        _add_commit(clone_path)
        mock_refresh_scheduler.trigger_refresh_for_repo.side_effect = RuntimeError(
            "scheduler down"
        )

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert "repo-fail" in result.stale_indexes_found
        assert "repo-fail" in result.stale_indexes_failed
        assert "repo-fail" not in result.stale_indexes_refreshed

    def test_unknown_commit_treated_as_stale(self, manager, mock_refresh_scheduler):
        """The literal ``"unknown"`` in metadata always triggers a refresh."""
        clone_path = self._register_repo(manager, "repo-unknown")
        _write_metadata(clone_path, "unknown")

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert "repo-unknown" in result.stale_indexes_found
        assert "repo-unknown" in result.stale_indexes_refreshed

    def test_prefix_match_not_stale(self, manager, mock_refresh_scheduler):
        """A 7+ char hex prefix of HEAD is treated as matching (Bug #1591)."""
        clone_path = self._register_repo(manager, "repo-prefix")
        full_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Write only the first 8 characters as the recorded commit
        _write_metadata(clone_path, full_sha[:8])

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert result.stale_indexes_found == []
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_multiple_repos_mixed_staleness(self, manager, mock_refresh_scheduler):
        """Multiple repos: only stale ones trigger refresh, current ones skipped."""
        # Repo A: stale
        clone_a = self._register_repo(manager, "repo-a")
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_a,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _write_metadata(clone_a, old_sha)
        _add_commit(clone_a)

        # Repo B: current
        clone_b = self._register_repo(manager, "repo-b")
        current_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_b,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _write_metadata(clone_b, current_sha)

        # Repo C: no metadata
        self._register_repo(manager, "repo-c")

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        assert "repo-a" in result.stale_indexes_found
        assert "repo-a" in result.stale_indexes_refreshed
        assert "repo-b" not in result.stale_indexes_found
        assert "repo-c" not in result.stale_indexes_found
        assert mock_refresh_scheduler.trigger_refresh_for_repo.call_count == 1

    def test_stale_but_not_globally_active_skipped(
        self, manager, mock_refresh_scheduler
    ):
        """A stale repo that is NOT globally active must NOT trigger a refresh.

        ``trigger_refresh_for_repo`` resolves the alias via
        ``_resolve_global_alias()`` which raises ``ValueError`` for any alias
        not registered in the global registry. Pass 4 must gate on
        ``is_globally_active()`` -- the same way Pass 3 does -- to avoid
        hitting that error on every startup for every non-global repo.
        """
        clone_path = self._register_repo(
            manager, "repo-local-only", globally_active=False
        )
        old_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Write stale metadata, then advance HEAD -- genuinely stale
        _write_metadata(clone_path, old_sha)
        _add_commit(clone_path)

        result = reconcile_golden_repo_registry(
            manager, refresh_scheduler=mock_refresh_scheduler
        )

        # The repo should be healthy (clone exists) but Pass 4 must skip it
        assert result.healthy_count == 1
        assert result.stale_indexes_found == []
        assert result.stale_indexes_refreshed == []
        assert result.stale_indexes_failed == []
        mock_refresh_scheduler.trigger_refresh_for_repo.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestIndexCommitMatchesHead:
    """Unit tests for the prefix-tolerant SHA comparison."""

    def test_exact_match(self):
        sha = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head(sha, sha) is True

    def test_case_insensitive(self):
        sha_lower = "abc123def456789012345678901234567890abcd"
        sha_upper = "ABC123DEF456789012345678901234567890ABCD"
        assert _index_commit_matches_head(sha_lower, sha_upper) is True

    def test_prefix_7_chars_matches(self):
        full = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head("abc123d", full) is True

    def test_prefix_8_chars_matches(self):
        full = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head("abc123de", full) is True

    def test_prefix_6_chars_too_short(self):
        full = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head("abc123", full) is False

    def test_unknown_always_stale(self):
        full = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head("unknown", full) is False

    def test_unknown_case_insensitive(self):
        full = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head("Unknown", full) is False
        assert _index_commit_matches_head("UNKNOWN", full) is False

    def test_different_sha_no_match(self):
        sha_a = "abc123def456789012345678901234567890abcd"
        sha_b = "fedcba9876543210fedcba9876543210fedcba98"
        assert _index_commit_matches_head(sha_a, sha_b) is False

    def test_whitespace_trimmed(self):
        full = "abc123def456789012345678901234567890abcd"
        assert _index_commit_matches_head(f"  {full}  ", full) is True


class TestGetCloneGitHead:
    """Unit tests for _get_clone_git_head."""

    def test_valid_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sha = _init_git_repo(tmpdir)
            assert _get_clone_git_head(tmpdir) == sha

    def test_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert _get_clone_git_head(tmpdir) is None

    def test_nonexistent_path(self):
        assert _get_clone_git_head("/nonexistent/path/that/does/not/exist") is None
