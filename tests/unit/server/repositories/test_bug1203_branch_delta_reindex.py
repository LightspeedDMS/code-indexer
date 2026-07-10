"""
Unit tests for Bug #1203 — Activation never runs branch-aware reindex.

Tests the THREE wiring sites:
1. _do_activate_repository: branch-delta reindex runs as final phase for non-default branch
2. switch_branch: branch-delta reindex runs after checkout for non-default branch
3. sync_with_golden_repository: branch-delta reindex runs after merge for non-default branch

Skip guards:
- Default-branch activation triggers NO reindex (byte-identical CoW index)
- *-global repos trigger NO reindex

Failure handling:
- If post-activation reindex fails, the activation job fails (correctness-first)
- If switch_branch reindex fails, raise GitOperationError
- If sync reindex fails, raise GitOperationError
"""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from src.code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_golden_repo(alias: str, default_branch: str = "main") -> GoldenRepo:
    return GoldenRepo(
        alias=alias,
        repo_url=f"https://github.com/example/{alias}.git",
        default_branch=default_branch,
        clone_path=f"/fake/golden/{alias}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_golden_repo_manager_mock(golden_repo: GoldenRepo) -> MagicMock:
    mock = MagicMock(spec=GoldenRepoManager)
    mock.get_golden_repo.return_value = golden_repo
    mock.get_actual_repo_path.return_value = golden_repo.clone_path
    return mock


def _make_bgm_mock() -> MagicMock:
    mock = MagicMock()
    mock.submit_job.return_value = "job-001"
    return mock


def _make_index_manager_mock() -> MagicMock:
    """Return a mock for the index manager with run_branch_delta_index (public API)."""
    mock = MagicMock()
    # run_branch_delta_index is the public method called by ARM._run_branch_delta_index
    # (Bug #1203 MEDIUM 1: replaced private _execute_semantic_indexing cross-class call)
    mock.run_branch_delta_index.return_value = None
    return mock


def _make_clone_backend_mock() -> MagicMock:
    """
    Return a clone_backend mock whose create_clone_at_path does a real
    shutil.copytree so git operations inside _do_activate_repository work.
    """
    mock = MagicMock()

    def _real_clone(source_path: str, dest_path: str, **kwargs: Any) -> None:
        shutil.copytree(source_path, dest_path, symlinks=True)

    mock.create_clone_at_path.side_effect = _real_clone
    return mock


# ---------------------------------------------------------------------------
# Fixture: real git repo that can be CoW-cloned cheaply (cp -r)
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def real_git_repo(temp_dir):
    """
    Create a real git repository with two branches (main, feature).
    Returns (repo_path, default_branch, feature_branch).
    """
    repo_path = os.path.join(temp_dir, "golden-repos", "myrepo")
    os.makedirs(repo_path)

    def git(*args, cwd=repo_path):
        subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@test.com")
    git("config", "user.name", "T")

    # Create .code-indexer/config.json so CoW clone has it
    code_indexer_dir = os.path.join(repo_path, ".code-indexer")
    os.makedirs(code_indexer_dir, exist_ok=True)
    with open(os.path.join(code_indexer_dir, "config.json"), "w") as f:
        json.dump({"codebase_dir": repo_path}, f)

    readme = os.path.join(repo_path, "README.md")
    with open(readme, "w") as f:
        f.write("main branch content\n")
    git("add", ".")
    git("commit", "-m", "Initial commit")

    # Create feature branch with different content
    git("checkout", "-b", "feature/new-thing")
    with open(readme, "w") as f:
        f.write("feature branch content\n")
    git("add", ".")
    git("commit", "-m", "Feature work")

    # Return to main
    git("checkout", "main")

    return repo_path, "main", "feature/new-thing"


@pytest.fixture()
def activated_repo_dir(temp_dir):
    """Return the activated-repos directory under temp_dir."""
    d = os.path.join(temp_dir, "activated-repos")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Helper: build ActivatedRepoManager with injected index_manager
# ---------------------------------------------------------------------------


def _build_arm(
    temp_dir: str,
    golden_repo: GoldenRepo,
    index_manager: Optional[MagicMock] = None,
    bgm: Optional[MagicMock] = None,
) -> ActivatedRepoManager:
    grm = _make_golden_repo_manager_mock(golden_repo)
    if bgm is None:
        bgm = _make_bgm_mock()
    arm = ActivatedRepoManager(
        data_dir=temp_dir,
        golden_repo_manager=grm,
        background_job_manager=bgm,
        index_manager=index_manager,
    )
    return arm


# ===========================================================================
# 1. _do_activate_repository — non-default branch triggers reindex
# ===========================================================================


def _cow_clone_side_effect(golden_repo_path: str) -> Any:
    """
    Return a side_effect function for mocking _clone_with_copy_on_write.
    Performs a real git clone so subsequent git operations inside
    _do_activate_repository work correctly.
    """

    def _do_clone(source_path: str, dest_path: str, cancel_check: Any = None) -> bool:
        subprocess.run(
            ["git", "clone", golden_repo_path, dest_path],
            check=True,
            capture_output=True,
        )
        # Fetch all remote branches so git checkout -B branch origin/branch works
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=dest_path,
            check=True,
            capture_output=True,
        )
        # Create a minimal .code-indexer dir so fix-config-like steps don't fail
        code_indexer_dir = os.path.join(dest_path, ".code-indexer")
        os.makedirs(code_indexer_dir, exist_ok=True)
        return True

    return _do_clone


class TestDoActivateRepositoryBranchDeltaReindex:
    """Tests for _do_activate_repository wiring site."""

    def _build_arm_with_real_clone(
        self,
        temp_dir: str,
        golden_repo: GoldenRepo,
        index_manager: Optional[MagicMock] = None,
    ) -> "ActivatedRepoManager":
        """Build ARM with _clone_with_copy_on_write mocked to do a real git clone."""
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
            index_manager=index_manager,
        )
        # Mock _clone_with_copy_on_write to do a real git clone
        arm._clone_with_copy_on_write = MagicMock(  # type: ignore[method-assign]
            side_effect=_cow_clone_side_effect(golden_repo.clone_path)
        )
        return arm

    def test_non_default_branch_triggers_reindex(self, temp_dir, real_git_repo):
        """
        When activating a non-default branch, _execute_semantic_indexing is called
        on the injected index_manager as the final phase.
        """
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        index_manager = _make_index_manager_mock()
        arm = self._build_arm_with_real_clone(temp_dir, golden_repo, index_manager)

        result = arm._do_activate_repository(
            username="user1",
            golden_repo_alias="myrepo",
            branch_name=feature_branch,
            user_alias="myrepo",
        )

        assert result["success"] is True
        # Reindex MUST have been called via the public API
        index_manager.run_branch_delta_index.assert_called_once()
        # Verify it was called with the activated repo path (not the golden path)
        call_args = index_manager.run_branch_delta_index.call_args
        called_path = call_args[0][0]
        assert "myrepo" in called_path
        assert called_path != repo_path  # Must be the activated clone, not golden

    def test_default_branch_does_not_trigger_reindex(self, temp_dir, real_git_repo):
        """
        When activating the DEFAULT branch, NO reindex is triggered.
        The CoW clone already has the correct index.
        """
        repo_path, default_branch, _ = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        index_manager = _make_index_manager_mock()
        arm = self._build_arm_with_real_clone(temp_dir, golden_repo, index_manager)

        result = arm._do_activate_repository(
            username="user1",
            golden_repo_alias="myrepo",
            branch_name=default_branch,
            user_alias="myrepo",
        )

        assert result["success"] is True
        # Reindex MUST NOT have been called for default branch
        index_manager.run_branch_delta_index.assert_not_called()

    def test_global_repo_does_not_trigger_reindex(self, temp_dir, real_git_repo):
        """
        Repos with alias ending in '-global' never trigger reindex.
        """
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        index_manager = _make_index_manager_mock()
        arm = self._build_arm_with_real_clone(temp_dir, golden_repo, index_manager)

        # Use a *-global user alias
        result = arm._do_activate_repository(
            username="user1",
            golden_repo_alias="myrepo",
            branch_name=feature_branch,
            user_alias="myrepo-global",
        )

        assert result["success"] is True
        # Reindex MUST NOT be called for *-global repos
        index_manager.run_branch_delta_index.assert_not_called()

    def test_reindex_failure_propagates_as_activation_error(
        self, temp_dir, real_git_repo
    ):
        """
        If post-activation reindex fails, _do_activate_repository raises
        ActivatedRepoError (correctness-first per Bug #1203 spec).
        """
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        index_manager = _make_index_manager_mock()
        index_manager.run_branch_delta_index.side_effect = RuntimeError(
            "cidx index failed: provider unavailable"
        )
        arm = self._build_arm_with_real_clone(temp_dir, golden_repo, index_manager)

        with pytest.raises(ActivatedRepoError) as exc_info:
            arm._do_activate_repository(
                username="user1",
                golden_repo_alias="myrepo",
                branch_name=feature_branch,
                user_alias="myrepo",
            )
        assert (
            "reindex" in str(exc_info.value).lower()
            or "index" in str(exc_info.value).lower()
        )

    def test_no_index_manager_does_not_crash(self, temp_dir, real_git_repo):
        """
        When no index_manager is injected (legacy callers), activation succeeds
        and no reindex is attempted (safe degradation for existing deployments
        until index_manager is wired into lifespan).
        """
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        arm = self._build_arm_with_real_clone(temp_dir, golden_repo)

        result = arm._do_activate_repository(
            username="user1",
            golden_repo_alias="myrepo",
            branch_name=feature_branch,
            user_alias="myrepo",
        )

        assert result["success"] is True


# ===========================================================================
# 2. switch_branch — non-default branch triggers reindex
# ===========================================================================


class TestSwitchBranchDeltaReindex:
    """Tests for switch_branch wiring site."""

    @pytest.fixture()
    def activated_repo(self, temp_dir, real_git_repo):
        """
        Create an activated repo on main branch so we can switch from it.
        Returns (arm, activated_path, golden_repo).
        """
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
        )

        # Manually create the activated repo structure (clone + metadata)
        user_dir = os.path.join(arm.activated_repos_dir, "user1")
        os.makedirs(user_dir, exist_ok=True)
        activated_path = os.path.join(user_dir, "myrepo")

        # Clone from golden repo
        subprocess.run(
            ["git", "clone", repo_path, activated_path],
            check=True,
            capture_output=True,
        )
        # Fetch all branches
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )

        # Write metadata
        metadata = {
            "username": "user1",
            "user_alias": "myrepo",
            "golden_repo_alias": "myrepo",
            "current_branch": default_branch,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "last_accessed": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(user_dir, "myrepo_metadata.json"), "w") as f:
            json.dump(metadata, f)

        return arm, activated_path, golden_repo

    def test_switch_to_non_default_branch_triggers_reindex(
        self, temp_dir, real_git_repo, activated_repo
    ):
        """
        switch_branch to a non-default branch invokes _execute_semantic_indexing.
        """
        _, _, feature_branch = real_git_repo
        arm, activated_path, golden_repo = activated_repo

        index_manager = _make_index_manager_mock()
        arm._index_manager = index_manager

        result = arm.switch_branch("user1", "myrepo", feature_branch)

        assert result["success"] is True
        index_manager.run_branch_delta_index.assert_called_once()

    def test_switch_to_default_branch_no_reindex(
        self, temp_dir, real_git_repo, activated_repo
    ):
        """
        switch_branch to the DEFAULT branch does NOT invoke reindex.
        """
        _, default_branch, _ = real_git_repo
        arm, activated_path, golden_repo = activated_repo

        index_manager = _make_index_manager_mock()
        arm._index_manager = index_manager

        # Switch to a feature branch first so we're not already on default
        _, _, feature_branch = real_git_repo
        arm.switch_branch("user1", "myrepo", feature_branch)
        index_manager.reset_mock()

        # Now switch back to default — no reindex expected
        result = arm.switch_branch("user1", "myrepo", default_branch)

        assert result["success"] is True
        index_manager.run_branch_delta_index.assert_not_called()

    def test_switch_reindex_failure_raises_activated_repo_error(
        self, temp_dir, real_git_repo, activated_repo
    ):
        """
        If post-switch reindex fails, switch_branch raises ActivatedRepoError.
        The outer except guard in switch_branch re-raises ActivatedRepoError
        as-is (isinstance check passes for both ActivatedRepoError and
        GitOperationError).
        """
        _, _, feature_branch = real_git_repo
        arm, _, _ = activated_repo

        index_manager = _make_index_manager_mock()
        index_manager.run_branch_delta_index.side_effect = RuntimeError(
            "cidx index timed out"
        )
        arm._index_manager = index_manager

        with pytest.raises(ActivatedRepoError) as exc_info:
            arm.switch_branch("user1", "myrepo", feature_branch)
        assert (
            "index" in str(exc_info.value).lower()
            or "reindex" in str(exc_info.value).lower()
        )

    def test_switch_global_repo_no_reindex(self, temp_dir, real_git_repo):
        """
        switch_branch on a *-global repo alias skips reindex.
        """
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
        )

        # Set up activated repo under *-global alias
        user_dir = os.path.join(arm.activated_repos_dir, "user1")
        os.makedirs(user_dir, exist_ok=True)
        activated_path = os.path.join(user_dir, "myrepo-global")
        subprocess.run(
            ["git", "clone", repo_path, activated_path],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )
        metadata = {
            "username": "user1",
            "user_alias": "myrepo-global",
            "golden_repo_alias": "myrepo",
            "current_branch": default_branch,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "last_accessed": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(user_dir, "myrepo-global_metadata.json"), "w") as f:
            json.dump(metadata, f)

        index_manager = _make_index_manager_mock()
        arm._index_manager = index_manager

        result = arm.switch_branch("user1", "myrepo-global", feature_branch)

        assert result["success"] is True
        # Global repo: no reindex
        index_manager.run_branch_delta_index.assert_not_called()


# ===========================================================================
# 3. sync_with_golden_repository — non-default branch triggers reindex
# ===========================================================================


class TestSyncWithGoldenRepositoryDeltaReindex:
    """Tests for sync_with_golden_repository wiring site."""

    @pytest.fixture()
    def golden_and_activated(self, temp_dir):
        """
        Create a golden repo + activated clone on a feature branch.
        The golden has new commits on feature branch that will be fetched.
        Returns (arm, golden_path, activated_path, feature_branch, default_branch).
        """
        golden_path = os.path.join(temp_dir, "golden-repos", "syncrepo")
        os.makedirs(golden_path)

        def git(*args, cwd=golden_path):
            subprocess.run(
                ["git"] + list(args), cwd=cwd, check=True, capture_output=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "t@test.com")
        git("config", "user.name", "T")

        # Create .code-indexer dir in golden
        code_indexer_dir = os.path.join(golden_path, ".code-indexer")
        os.makedirs(code_indexer_dir, exist_ok=True)
        with open(os.path.join(code_indexer_dir, "config.json"), "w") as f:
            json.dump({"codebase_dir": golden_path}, f)

        with open(os.path.join(golden_path, "base.py"), "w") as f:
            f.write("x = 1\n")
        git("add", ".")
        git("commit", "-m", "initial")

        git("checkout", "-b", "feature/sync-test")
        with open(os.path.join(golden_path, "base.py"), "w") as f:
            f.write("x = 2\n")
        git("add", ".")
        git("commit", "-m", "feature work")
        git("checkout", "main")

        # Create activated clone on feature branch
        user_dir = os.path.join(temp_dir, "activated-repos", "user1")
        os.makedirs(user_dir, exist_ok=True)
        activated_path = os.path.join(user_dir, "syncrepo")
        subprocess.run(
            ["git", "clone", golden_path, activated_path],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "feature/sync-test", "origin/feature/sync-test"],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )

        # Add "golden" remote pointing to golden repo (sync_with_golden uses this)
        subprocess.run(
            ["git", "remote", "add", "golden", golden_path],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )

        # Make a new commit in golden's feature branch to trigger sync changes
        subprocess.run(
            ["git", "checkout", "feature/sync-test"],
            cwd=golden_path,
            check=True,
            capture_output=True,
        )
        with open(os.path.join(golden_path, "newfile.py"), "w") as f:
            f.write("y = 3\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=golden_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "new feature commit"],
            cwd=golden_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=golden_path,
            check=True,
            capture_output=True,
        )

        # Write metadata for activated repo
        metadata = {
            "username": "user1",
            "user_alias": "syncrepo",
            "golden_repo_alias": "syncrepo",
            "current_branch": "feature/sync-test",
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "last_accessed": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(user_dir, "syncrepo_metadata.json"), "w") as f:
            json.dump(metadata, f)

        golden_repo = GoldenRepo(
            alias="syncrepo",
            repo_url="file://" + golden_path,
            default_branch="main",
            clone_path=golden_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
        )

        return arm, golden_path, activated_path, "feature/sync-test", "main"

    def test_sync_on_non_default_branch_triggers_reindex(self, golden_and_activated):
        """
        sync_with_golden_repository on a non-default branch triggers
        _execute_semantic_indexing after the merge.
        """
        arm, _, _, feature_branch, _ = golden_and_activated

        index_manager = _make_index_manager_mock()
        arm._index_manager = index_manager

        result = arm.sync_with_golden_repository("user1", "syncrepo")

        assert result["success"] is True
        assert result.get("changes_applied") is True
        index_manager.run_branch_delta_index.assert_called_once()

    def test_sync_already_up_to_date_no_reindex(self, golden_and_activated):
        """
        sync_with_golden_repository when already up to date does NOT trigger reindex.
        """
        arm, _, _, feature_branch, _ = golden_and_activated

        index_manager = _make_index_manager_mock()
        arm._index_manager = index_manager

        # First sync pulls in changes
        arm.sync_with_golden_repository("user1", "syncrepo")
        index_manager.reset_mock()

        # Second sync: already up to date — no reindex
        result = arm.sync_with_golden_repository("user1", "syncrepo")

        assert result["success"] is True
        assert result.get("changes_applied") is False
        index_manager.run_branch_delta_index.assert_not_called()

    def test_sync_reindex_failure_raises_activated_repo_error(
        self, golden_and_activated
    ):
        """
        If post-sync reindex fails, sync_with_golden_repository raises
        ActivatedRepoError. The outer except guard in sync re-raises
        ActivatedRepoError as-is (isinstance check passes for both
        ActivatedRepoError and GitOperationError).
        """
        arm, _, _, _, _ = golden_and_activated

        index_manager = _make_index_manager_mock()
        index_manager.run_branch_delta_index.side_effect = RuntimeError(
            "cidx index failed"
        )
        arm._index_manager = index_manager

        with pytest.raises(ActivatedRepoError) as exc_info:
            arm.sync_with_golden_repository("user1", "syncrepo")
        assert (
            "index" in str(exc_info.value).lower()
            or "reindex" in str(exc_info.value).lower()
        )

    def test_sync_global_repo_no_reindex(self, temp_dir):
        """
        sync_with_golden_repository on a *-global repo alias skips reindex.
        """
        golden_path = os.path.join(temp_dir, "golden-repos", "gr")
        os.makedirs(golden_path)

        def git(*args, cwd=golden_path):
            subprocess.run(
                ["git"] + list(args), cwd=cwd, check=True, capture_output=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "t@test.com")
        git("config", "user.name", "T")
        with open(os.path.join(golden_path, "f.py"), "w") as f:
            f.write("x=1\n")
        git("add", ".")
        git("commit", "-m", "init")
        git("checkout", "-b", "feat")
        with open(os.path.join(golden_path, "f.py"), "w") as f:
            f.write("x=2\n")
        git("add", ".")
        git("commit", "-m", "feat")
        git("checkout", "main")

        # Make new commit on feat in golden to trigger sync
        git("checkout", "feat")
        with open(os.path.join(golden_path, "g.py"), "w") as f:
            f.write("y=1\n")
        git("add", ".")
        git("commit", "-m", "new")
        git("checkout", "main")

        user_dir = os.path.join(temp_dir, "activated-repos", "user1")
        os.makedirs(user_dir, exist_ok=True)
        activated_path = os.path.join(user_dir, "gr-global")
        subprocess.run(
            ["git", "clone", golden_path, activated_path],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "feat", "origin/feat"],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "golden", golden_path],
            cwd=activated_path,
            check=True,
            capture_output=True,
        )

        metadata = {
            "username": "user1",
            "user_alias": "gr-global",
            "golden_repo_alias": "gr",
            "current_branch": "feat",
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "last_accessed": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(user_dir, "gr-global_metadata.json"), "w") as f:
            json.dump(metadata, f)

        golden_repo = GoldenRepo(
            alias="gr",
            repo_url="file://" + golden_path,
            default_branch="main",
            clone_path=golden_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
        )

        index_manager = _make_index_manager_mock()
        arm._index_manager = index_manager

        result = arm.sync_with_golden_repository("user1", "gr-global")

        assert result["success"] is True
        # Global repo: no reindex
        index_manager.run_branch_delta_index.assert_not_called()


# ===========================================================================
# 4. Constructor: index_manager parameter accepted
# ===========================================================================


class TestActivatedRepoManagerIndexManagerConstructor:
    """Tests that ActivatedRepoManager accepts index_manager parameter."""

    def test_accepts_index_manager_kwarg(self, temp_dir):
        """Constructor accepts index_manager without error."""
        index_manager = _make_index_manager_mock()
        grm = MagicMock(spec=GoldenRepoManager)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
            index_manager=index_manager,
        )
        assert arm._index_manager is index_manager

    def test_default_index_manager_is_none(self, temp_dir):
        """When not provided, _index_manager defaults to None."""
        grm = MagicMock(spec=GoldenRepoManager)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
        )
        assert arm._index_manager is None


# ===========================================================================
# 5. Orphaned-clone cleanup on activation reindex failure (MEDIUM 2)
# ===========================================================================


class TestActivationOrphanCleanup:
    """When post-activation reindex fails, the orphaned CoW clone is removed.

    Bug #1203 MEDIUM 2: reindex runs at progress ~88 BEFORE metadata is
    written.  A failure leaves an orphaned clone with no metadata.  The fix
    must clean up the clone directory before re-raising ActivatedRepoError so
    a failed activation does not leak disk space.
    """

    def test_orphaned_clone_removed_on_reindex_failure(self, temp_dir, real_git_repo):
        """If post-activation reindex fails, the CoW clone directory is removed."""
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        index_manager = _make_index_manager_mock()
        index_manager.run_branch_delta_index.side_effect = RuntimeError(
            "cidx index failed: provider unavailable"
        )
        # Use the same test-infrastructure seam as TestDoActivateRepositoryBranchDeltaReindex:
        # _clone_with_copy_on_write is replaced with a real git clone so the rest of
        # _do_activate_repository (the code under test) can exercise git operations.
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
            index_manager=index_manager,
        )
        arm._clone_with_copy_on_write = MagicMock(  # type: ignore[method-assign]
            side_effect=_cow_clone_side_effect(golden_repo.clone_path)
        )

        with pytest.raises(ActivatedRepoError):
            arm._do_activate_repository(
                username="user1",
                golden_repo_alias="myrepo",
                branch_name=feature_branch,
                user_alias="myrepo",
            )

        expected_clone_dir = os.path.join(arm.activated_repos_dir, "user1", "myrepo")
        assert not os.path.exists(expected_clone_dir), (
            f"Orphaned clone directory was NOT removed after reindex failure: "
            f"{expected_clone_dir}"
        )

    def test_successful_reindex_leaves_clone_intact(self, temp_dir, real_git_repo):
        """Sanity check: when reindex succeeds, the clone directory remains."""
        repo_path, default_branch, feature_branch = real_git_repo
        golden_repo = GoldenRepo(
            alias="myrepo",
            repo_url="file://" + repo_path,
            default_branch=default_branch,
            clone_path=repo_path,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        index_manager = _make_index_manager_mock()
        grm = _make_golden_repo_manager_mock(golden_repo)
        arm = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=grm,
            background_job_manager=_make_bgm_mock(),
            index_manager=index_manager,
        )
        arm._clone_with_copy_on_write = MagicMock(  # type: ignore[method-assign]
            side_effect=_cow_clone_side_effect(golden_repo.clone_path)
        )

        result = arm._do_activate_repository(
            username="user1",
            golden_repo_alias="myrepo",
            branch_name=feature_branch,
            user_alias="myrepo",
        )

        assert result["success"] is True
        expected_clone_dir = os.path.join(arm.activated_repos_dir, "user1", "myrepo")
        assert os.path.isdir(expected_clone_dir), (
            "Clone directory was removed even on successful activation"
        )


# ===========================================================================
# 6. Cache invalidation after branch-delta reindex
# ===========================================================================


class TestCacheInvalidationAfterBranchDeltaReindex:
    """After a successful branch-delta reindex, HNSW and id_index caches are evicted.

    Bug #1203 stale-cache fix: the `cidx index` subprocess rewrites on-disk HNSW
    index files.  The server's in-memory HNSW cache and id_index cache still hold
    stale entries keyed by collection_path (= <repo_root>/.code-indexer/index/<coll>).
    `_run_branch_delta_index` must call `invalidate_prefix(<index_base>)` on both
    caches so the next query reloads from disk.

    FTS has NO in-process cache (TantivyIndexManager is created fresh per query
    from the on-disk directory) — no FTS invalidation is needed or tested here.

    These tests use REAL cache instances and REAL entry insertion (not mocks) so
    they FAIL against the old wrong-key `invalidate(repo_path)` code and PASS
    only when `invalidate_prefix(<repo_root>/.code-indexer/index)` is called.
    """

    def _make_real_caches(self):
        """Return fresh, isolated HNSWIndexCache and IdIndexCache instances."""
        from src.code_indexer.server.cache.hnsw_index_cache import (
            HNSWIndexCache,
            HNSWIndexCacheConfig,
        )
        from src.code_indexer.server.cache.id_index_cache import (
            IdIndexCache,
            IdIndexCacheConfig,
        )

        hnsw_cache = HNSWIndexCache(
            config=HNSWIndexCacheConfig(ttl_minutes=10, max_cache_size_mb=256)
        )
        id_cache = IdIndexCache(
            config=IdIndexCacheConfig(ttl_minutes=10, max_entries=200)
        )
        return hnsw_cache, id_cache

    def _insert_collection_entry(self, hnsw_cache, id_cache, collection_path_str):
        """Insert sentinel entries at collection_path_str into both real caches.

        Mirrors the key format used by the query path:
          cache_key = str(collection_path.resolve())   (filesystem_vector_store.py:2555)
        """
        key = str(collection_path_str)
        # HNSW: store a sentinel object as the "index" and "id_mapping"
        hnsw_cache.get_or_load(key, lambda: (object(), {}))
        # id_index: store a sentinel dict
        id_cache.get_or_load(key, lambda: {"sentinel": True})

    def test_real_cache_entries_evicted_after_successful_reindex(self, tmp_path):
        """Real cache entries at collection-path keys are evicted on success.

        1. Insert entries at <repo_root>/.code-indexer/index/<collection> into
           real HNSW and id_index caches.
        2. Run _run_branch_delta_index (reindex mocked to succeed — no subprocess).
        3. Assert both cache entries are gone (invalidated by invalidate_prefix).

        This test FAILS against the old `invalidate(repo_path)` code (wrong key,
        entry remains) and PASSES only after the correct `invalidate_prefix` fix.
        """
        from unittest.mock import MagicMock, patch

        repo_root = tmp_path / "my-repo"
        index_base = repo_root / ".code-indexer" / "index"
        collection_path = index_base / "voyage-code-3"
        collection_key = str(collection_path.resolve())

        hnsw_cache, id_cache = self._make_real_caches()
        self._insert_collection_entry(hnsw_cache, id_cache, collection_key)

        # Confirm entries are present before reindex
        assert hnsw_cache.get_stats().cached_repositories == 1, (
            "HNSW entry not inserted"
        )
        assert len(id_cache._cache) == 1, "id_index entry not inserted"

        index_manager = _make_index_manager_mock()  # run_branch_delta_index -> None
        grm = MagicMock()
        arm = ActivatedRepoManager(
            data_dir=str(tmp_path),
            golden_repo_manager=grm,
            index_manager=index_manager,
        )

        with (
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager.get_global_cache",
                return_value=hnsw_cache,
            ),
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager.get_global_id_index_cache",
                return_value=id_cache,
            ),
        ):
            arm._run_branch_delta_index(str(repo_root), "myrepo")

        # Both cache entries must be evicted so the next query reloads from disk
        assert hnsw_cache.get_stats().cached_repositories == 0, (
            "HNSW cache entry NOT evicted after branch-delta reindex. "
            "invalidate_prefix must be called with <repo>/.code-indexer/index, "
            "not the repo root."
        )
        assert len(id_cache._cache) == 0, (
            "id_index cache entry NOT evicted after branch-delta reindex. "
            "invalidate_prefix must be called with <repo>/.code-indexer/index, "
            "not the repo root."
        )

    def test_real_cache_entries_intact_on_reindex_failure(self, tmp_path):
        """Real cache entries remain when reindex fails — nothing new on disk.

        1. Insert entries at the correct collection-path key.
        2. Run _run_branch_delta_index with reindex raising RuntimeError.
        3. Assert both cache entries are still present (no stale eviction).
        """
        from unittest.mock import MagicMock, patch

        repo_root = tmp_path / "my-repo"
        index_base = repo_root / ".code-indexer" / "index"
        collection_path = index_base / "voyage-code-3"
        collection_key = str(collection_path.resolve())

        hnsw_cache, id_cache = self._make_real_caches()
        self._insert_collection_entry(hnsw_cache, id_cache, collection_key)

        index_manager = _make_index_manager_mock()
        index_manager.run_branch_delta_index.side_effect = RuntimeError("cidx failed")
        grm = MagicMock()
        arm = ActivatedRepoManager(
            data_dir=str(tmp_path),
            golden_repo_manager=grm,
            index_manager=index_manager,
        )

        with (
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager.get_global_cache",
                return_value=hnsw_cache,
            ),
            patch(
                "src.code_indexer.server.repositories.activated_repo_manager.get_global_id_index_cache",
                return_value=id_cache,
            ),
        ):
            with pytest.raises(ActivatedRepoError):
                arm._run_branch_delta_index(str(repo_root), "myrepo")

        # Entries must remain intact — reindex failed, disk is unchanged
        assert hnsw_cache.get_stats().cached_repositories == 1, (
            "HNSW entry wrongly evicted on failure"
        )
        assert len(id_cache._cache) == 1, "id_index entry wrongly evicted on failure"
