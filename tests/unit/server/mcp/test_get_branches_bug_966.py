"""
Unit tests for bug #966: get_branches MCP tool only returns registration branch
for golden repos — all other remote branches are invisible to AI agents.

Tests cover all 5 issues from the bug report:
  Issue 1: list_branches iterates only local heads (fixes: also iterate remote refs)
  Issue 2: _resolve_branch_repo_path returns versioned snapshot path (fixes: use base clone)
  Issue 3: repo.remotes.origin may not exist for local:// repos (fixes: guard with check)
  Issue 4: origin/HEAD symbolic ref must be filtered (fixes: skip refs ending in /HEAD)
  Issue 5: _create_branch_info must handle RemoteReference objects (fixes: skip tracking_branch)
"""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from git import Repo

from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.server.services.branch_service import BranchService


class _GitRepoTestBase:
    """Shared setup/teardown for tests that need a real local git repo."""

    def setup_method(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.real_repo = Repo.init(self.temp_dir)
        self.real_repo.config_writer().set_value("user", "name", "Test User").release()
        self.real_repo.config_writer().set_value(
            "user", "email", "test@test.com"
        ).release()
        test_file = self.temp_dir / "test.py"
        test_file.write_text("x = 1")
        self.real_repo.index.add([str(test_file)])
        self.real_repo.index.commit("Initial commit")

    def teardown_method(self):
        self.real_repo.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_mock_repo(self, remotes=None) -> MagicMock:
        """Return a MagicMock that mimics git.Repo with controllable remotes.

        Patches code_indexer.server.services.branch_service.Repo so that
        BranchService.__init__ receives this mock when it calls Repo(path).
        remotes must be a list (possibly empty) — MagicMock attributes are
        plain Python attributes so assignment always works.
        """
        mock_repo = MagicMock()
        mock_repo.heads = self.real_repo.heads
        mock_repo.active_branch = self.real_repo.active_branch
        mock_repo.remotes = remotes if remotes is not None else []
        mock_repo.close.return_value = None
        return mock_repo


class TestListBranchesIncludesRemoteOnlyBranches(_GitRepoTestBase):
    """Issue 1: BranchService.list_branches must also return remote-only branches."""

    def test_list_branches_includes_remote_only_branches(self):
        """
        Given a repo with 1 local branch (master) and 2 remote-only branches
        (origin/development, origin/staging) that have no local counterpart,
        list_branches must return all 3 unique branch names.
        """
        commit = self.real_repo.head.commit

        mock_dev_ref = MagicMock()
        mock_dev_ref.name = "origin/development"
        mock_dev_ref.commit = commit

        mock_staging_ref = MagicMock()
        mock_staging_ref.name = "origin/staging"
        mock_staging_ref.commit = commit

        mock_origin = MagicMock()
        mock_origin.name = "origin"
        mock_origin.refs = [mock_dev_ref, mock_staging_ref]

        mock_repo = self._make_mock_repo(remotes=[mock_origin])

        git_topo = GitTopologyService(self.temp_dir)
        with patch(
            "code_indexer.server.services.branch_service.Repo",
            return_value=mock_repo,
        ):
            with BranchService(git_topology_service=git_topo) as svc:
                branches = svc.list_branches()

        branch_names = {b.name for b in branches}
        assert "development" in branch_names, (
            f"Remote-only 'development' branch missing. Got: {branch_names}"
        )
        assert "staging" in branch_names, (
            f"Remote-only 'staging' branch missing. Got: {branch_names}"
        )


class TestListBranchesFiltersOriginHead(_GitRepoTestBase):
    """Issue 4: origin/HEAD symbolic ref must be filtered out."""

    def test_list_branches_filters_origin_head(self):
        """
        Given a remote with an origin/HEAD symbolic ref among its refs,
        list_branches must NOT include a branch named 'HEAD' in the output.
        """
        commit = self.real_repo.head.commit

        mock_head_ref = MagicMock()
        mock_head_ref.name = "origin/HEAD"
        mock_head_ref.commit = commit

        mock_dev_ref = MagicMock()
        mock_dev_ref.name = "origin/development"
        mock_dev_ref.commit = commit

        mock_origin = MagicMock()
        mock_origin.name = "origin"
        mock_origin.refs = [mock_head_ref, mock_dev_ref]

        mock_repo = self._make_mock_repo(remotes=[mock_origin])

        git_topo = GitTopologyService(self.temp_dir)
        with patch(
            "code_indexer.server.services.branch_service.Repo",
            return_value=mock_repo,
        ):
            with BranchService(git_topology_service=git_topo) as svc:
                branches = svc.list_branches()

        branch_names = {b.name for b in branches}
        assert "HEAD" not in branch_names, (
            f"origin/HEAD must be filtered but 'HEAD' appears in: {branch_names}"
        )
        assert "development" in branch_names, (
            f"'development' should still appear. Got: {branch_names}"
        )


class TestListBranchesNoRemoteDoesNotCrash(_GitRepoTestBase):
    """Issue 3: repos with no remotes (e.g. cidx-meta local://) must not crash."""

    def test_list_branches_no_remote_does_not_crash(self):
        """
        Given a local repo with no remotes (empty repo.remotes),
        list_branches must return only local branches without raising any exception.
        """
        mock_repo = self._make_mock_repo(remotes=[])

        git_topo = GitTopologyService(self.temp_dir)
        with patch(
            "code_indexer.server.services.branch_service.Repo",
            return_value=mock_repo,
        ):
            with BranchService(git_topology_service=git_topo) as svc:
                branches = svc.list_branches()

        assert len(branches) >= 1, f"Expected at least 1 local branch, got: {branches}"


class TestListBranchesDeduplicatesLocalTakesPrecedence(_GitRepoTestBase):
    """Issue 1: deduplication — local branch takes precedence over remote."""

    def test_list_branches_deduplicates_local_takes_precedence(self):
        """
        Given a repo with local 'master' AND remote 'origin/master',
        list_branches must return only ONE 'master' entry (no duplicate).
        Local branch info takes precedence over the remote entry.
        """
        commit = self.real_repo.head.commit
        local_branch_name = self.real_repo.active_branch.name

        mock_master_ref = MagicMock()
        mock_master_ref.name = f"origin/{local_branch_name}"
        mock_master_ref.commit = commit

        mock_origin = MagicMock()
        mock_origin.name = "origin"
        mock_origin.refs = [mock_master_ref]

        mock_repo = self._make_mock_repo(remotes=[mock_origin])

        git_topo = GitTopologyService(self.temp_dir)
        with patch(
            "code_indexer.server.services.branch_service.Repo",
            return_value=mock_repo,
        ):
            with BranchService(git_topology_service=git_topo) as svc:
                branches = svc.list_branches()

        master_entries = [b for b in branches if b.name == local_branch_name]
        assert len(master_entries) == 1, (
            f"Expected exactly 1 '{local_branch_name}' entry but got "
            f"{len(master_entries)}. All branches: {[b.name for b in branches]}"
        )


class TestCreateBranchInfoHandlesRemoteReference(_GitRepoTestBase):
    """Issue 5: _create_branch_info must handle RemoteReference objects."""

    def test_create_branch_info_handles_remote_reference_without_crash(self):
        """
        Given a RemoteReference object (not a Head) passed to _create_branch_info,
        the method must return a valid BranchInfo without calling .tracking_branch()
        (RemoteReference has different semantics for tracking_branch).
        remote_tracking field must be None for remote refs when include_remote=False.
        """
        from git import RemoteReference

        real_commit = self.real_repo.head.commit

        # Use a free-standing MagicMock for commit so hexsha/etc. are freely assignable.
        # Mock(spec=RemoteReference) causes commit child attributes to be spec-constrained
        # which makes hexsha (a real property) unwritable.
        mock_commit = MagicMock()
        mock_commit.hexsha = real_commit.hexsha
        mock_commit.message = "Initial commit"
        mock_commit.author.name = "Test User"
        mock_commit.committed_datetime = real_commit.committed_datetime

        mock_remote_ref = MagicMock(spec=RemoteReference)
        mock_remote_ref.name = "development"
        mock_remote_ref.commit = mock_commit

        git_topo = GitTopologyService(self.temp_dir)
        with BranchService(git_topology_service=git_topo) as svc:
            # Must not raise even when the ref is a RemoteReference type
            branch_info = svc._create_branch_info(
                mock_remote_ref, is_current=False, include_remote=False
            )

        assert branch_info is not None
        assert branch_info.name == "development"
        assert branch_info.remote_tracking is None

    def test_create_branch_info_remote_reference_with_include_remote_does_not_crash(
        self,
    ):
        """
        Given a RemoteReference passed to _create_branch_info with include_remote=True,
        the method must NOT call .tracking_branch() on it (different semantics).
        It must return BranchInfo with remote_tracking=None (remote refs do not track
        another remote).
        """
        from git import RemoteReference

        real_commit = self.real_repo.head.commit

        mock_commit = MagicMock()
        mock_commit.hexsha = real_commit.hexsha
        mock_commit.message = "Initial commit"
        mock_commit.author.name = "Test User"
        mock_commit.committed_datetime = real_commit.committed_datetime

        mock_remote_ref = MagicMock(spec=RemoteReference)
        mock_remote_ref.name = "staging"
        mock_remote_ref.commit = mock_commit

        git_topo = GitTopologyService(self.temp_dir)
        with BranchService(git_topology_service=git_topo) as svc:
            branch_info = svc._create_branch_info(
                mock_remote_ref, is_current=False, include_remote=True
            )

        assert branch_info is not None
        assert branch_info.name == "staging"
        # RemoteReference objects do not have a tracking branch of their own
        assert branch_info.remote_tracking is None


class TestResolveBranchRepoPathUsesBaseCloneForGlobal:
    """Issue 2: _resolve_branch_repo_path must use get_actual_repo_path for -global aliases."""

    def test_resolve_branch_repo_path_uses_base_clone_for_global(self):
        """
        Given a '-global' repository alias,
        _resolve_branch_repo_path must call golden_repo_manager.get_actual_repo_path()
        with the BASE alias (suffix stripped) so that the mutable base clone path
        (with fresh remote refs) is returned, not the frozen versioned snapshot.
        """
        from code_indexer.server.mcp.handlers.repos import _resolve_branch_repo_path

        base_clone_path = "/data/golden-repos/my-repo"
        alias = "my-repo-global"
        repo_entry = {"alias_name": alias}

        with (
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/data/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.repos._list_global_repos",
                return_value=[repo_entry],
            ),
            patch("code_indexer.server.mcp.handlers.repos._utils") as mock_utils,
            patch(
                "code_indexer.server.mcp.handlers.repos.AliasManager"
            ) as mock_alias_manager_cls,
        ):
            mock_grm = Mock()
            mock_grm.get_actual_repo_path.return_value = base_clone_path
            mock_utils.app_module.golden_repo_manager = mock_grm

            user = Mock()
            path, error = _resolve_branch_repo_path(alias, user)

        # Must have used get_actual_repo_path with BASE alias (suffix stripped),
        # not the full '-global'-suffixed alias.
        mock_grm.get_actual_repo_path.assert_called_once_with("my-repo")
        mock_alias_manager_cls.assert_not_called()
        assert path == base_clone_path
        assert error is None

    def test_resolve_branch_repo_path_fallback_on_not_found(self):
        """
        Given a '-global' alias where get_actual_repo_path raises GoldenRepoNotFoundError,
        _resolve_branch_repo_path must fall back to AliasManager.read_alias() and return
        the path it provides, without propagating the exception.
        """
        from code_indexer.server.mcp.handlers.repos import _resolve_branch_repo_path
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoNotFoundError,
        )

        fallback_path = "/data/golden-repos/.versioned/my-repo/v_123456"
        alias = "my-repo-global"
        repo_entry = {"alias_name": alias}

        with (
            patch(
                "code_indexer.server.mcp.handlers.repos._get_golden_repos_dir",
                return_value="/data/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.repos._list_global_repos",
                return_value=[repo_entry],
            ),
            patch("code_indexer.server.mcp.handlers.repos._utils") as mock_utils,
            patch(
                "code_indexer.server.mcp.handlers.repos.AliasManager"
            ) as mock_alias_manager_cls,
        ):
            mock_grm = Mock()
            mock_grm.get_actual_repo_path.side_effect = GoldenRepoNotFoundError(
                "my-repo not found"
            )
            mock_utils.app_module.golden_repo_manager = mock_grm

            mock_alias_manager_instance = Mock()
            mock_alias_manager_instance.read_alias.return_value = fallback_path
            mock_alias_manager_cls.return_value = mock_alias_manager_instance

            user = Mock()
            path, error = _resolve_branch_repo_path(alias, user)

        # get_actual_repo_path was called with base alias (suffix stripped)
        mock_grm.get_actual_repo_path.assert_called_once_with("my-repo")
        # AliasManager fallback was used with the full alias
        mock_alias_manager_instance.read_alias.assert_called_once_with(alias)
        assert path == fallback_path
        assert error is None
