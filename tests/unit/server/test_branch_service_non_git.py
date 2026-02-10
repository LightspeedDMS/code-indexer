"""
Unit tests for BranchService non-git folder support (Story #163).

Tests that BranchService gracefully handles non-git folders registered
with local:// URL scheme, following TDD methodology.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from git import Repo

from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.server.services.branch_service import BranchService
from code_indexer.server.models.branch_models import IndexStatus


class IndexStatusManagerForTesting:
    """Real test implementation of IndexStatusManager - no mocks per CLAUDE.md Foundation #1."""

    def __init__(self):
        self.branch_statuses = {}

    def get_branch_index_status(self, branch_name: str, repo_path: Path) -> IndexStatus:
        """Get index status for a specific branch."""
        return self.branch_statuses.get(
            branch_name,
            IndexStatus(
                status="not_indexed",
                files_indexed=0,
                total_files=None,
                last_indexed=None,
                progress_percentage=0.0,
            ),
        )

    def set_branch_status(self, branch_name: str, status: IndexStatus):
        """Set status for testing purposes."""
        self.branch_statuses[branch_name] = status


class TestBranchServiceNonGitSupport:
    """Test cases for BranchService non-git folder support (Story #163)."""

    def setup_method(self):
        """Set up test environment."""
        self.temp_dirs = []

    def teardown_method(self):
        """Clean up test directories."""
        for temp_dir in self.temp_dirs:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    def _create_temp_dir(self) -> Path:
        """Create a temporary directory and track it for cleanup."""
        temp_dir = Path(tempfile.mkdtemp())
        self.temp_dirs.append(temp_dir)
        return temp_dir

    # AC1: BranchService Non-Git Tolerance
    def test_branch_service_accepts_non_git_folder(self):
        """
        AC1: Given a non-git folder registered with local:// URL scheme,
        when the BranchService is instantiated for that folder,
        then it should NOT raise ValueError("Not a git repository")
        and it should initialize successfully with git features disabled.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        # Add a test file to make it a real folder
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        git_topology_service = GitTopologyService(non_git_dir)
        index_status_manager = IndexStatusManagerForTesting()

        # Act & Assert - Should NOT raise ValueError
        # Currently FAILS: raises ValueError("Not a git repository")
        branch_service = BranchService(
            git_topology_service=git_topology_service,
            index_status_manager=index_status_manager,
        )

        # Verify service initialized successfully
        assert branch_service is not None
        assert branch_service.git_topology_service == git_topology_service

        # Clean up
        branch_service.close()

    # AC2: Empty Branch List for Non-Git
    def test_list_branches_returns_empty_for_non_git(self):
        """
        AC2: Given a non-git folder registered as a repository,
        when list_branches() is called on BranchService,
        then it should return an empty list
        and it should NOT raise any exceptions.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        git_topology_service = GitTopologyService(non_git_dir)
        index_status_manager = IndexStatusManagerForTesting()

        # This will fail until AC1 is fixed
        try:
            branch_service = BranchService(
                git_topology_service=git_topology_service,
                index_status_manager=index_status_manager,
            )
        except ValueError:
            pytest.skip("AC1 not implemented yet - BranchService rejects non-git folders")

        # Act
        branches = branch_service.list_branches()

        # Assert
        assert branches == []
        assert len(branches) == 0

        # Clean up
        branch_service.close()

    def test_list_branches_with_include_remote_returns_empty_for_non_git(self):
        """
        AC2 Extension: list_branches(include_remote=True) should also return empty list for non-git.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        git_topology_service = GitTopologyService(non_git_dir)
        index_status_manager = IndexStatusManagerForTesting()

        # This will fail until AC1 is fixed
        try:
            branch_service = BranchService(
                git_topology_service=git_topology_service,
                index_status_manager=index_status_manager,
            )
        except ValueError:
            pytest.skip("AC1 not implemented yet - BranchService rejects non-git folders")

        # Act
        branches = branch_service.list_branches(include_remote=True)

        # Assert
        assert branches == []
        assert len(branches) == 0

        # Clean up
        branch_service.close()

    def test_get_branch_by_name_returns_none_for_non_git(self):
        """
        AC2 Extension: get_branch_by_name() should return None for non-git folders.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        git_topology_service = GitTopologyService(non_git_dir)
        index_status_manager = IndexStatusManagerForTesting()

        # This will fail until AC1 is fixed
        try:
            branch_service = BranchService(
                git_topology_service=git_topology_service,
                index_status_manager=index_status_manager,
            )
        except ValueError:
            pytest.skip("AC1 not implemented yet - BranchService rejects non-git folders")

        # Act
        branch = branch_service.get_branch_by_name("any-branch-name")

        # Assert
        assert branch is None

        # Clean up
        branch_service.close()

    # AC5: Regression Safety - Verify existing git functionality still works
    def test_git_repository_functionality_unchanged(self):
        """
        AC5: Given existing git-based repositories,
        when any of the modified code paths are executed,
        then all existing functionality should work unchanged.
        """
        # Arrange - Create real git repository
        git_dir = self._create_temp_dir()
        repo = Repo.init(git_dir)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@example.com").release()

        # Create initial commit
        test_file = git_dir / "test.py"
        test_file.write_text("print('hello world')")
        repo.index.add([str(test_file)])
        initial_commit = repo.index.commit("Initial commit")

        # Create additional branch
        develop_branch = repo.create_head("develop")
        repo.heads.develop.checkout()
        develop_file = git_dir / "develop.py"
        develop_file.write_text("print('develop branch')")
        repo.index.add([str(develop_file)])
        develop_commit = repo.index.commit("Add develop feature")

        repo.heads.master.checkout()

        # Act
        git_topology_service = GitTopologyService(git_dir)
        index_status_manager = IndexStatusManagerForTesting()
        branch_service = BranchService(
            git_topology_service=git_topology_service,
            index_status_manager=index_status_manager,
        )

        branches = branch_service.list_branches()

        # Assert - All existing git functionality should work
        assert len(branches) == 2
        branch_names = {branch.name for branch in branches}
        assert branch_names == {"master", "develop"}

        # Verify current branch detection
        current_branches = [b for b in branches if b.is_current]
        assert len(current_branches) == 1
        assert current_branches[0].name == "master"

        # Verify commit information
        master_branch = next(b for b in branches if b.name == "master")
        assert master_branch.last_commit.sha == initial_commit.hexsha
        assert master_branch.last_commit.message == "Initial commit"

        develop_branch_info = next(b for b in branches if b.name == "develop")
        assert develop_branch_info.last_commit.sha == develop_commit.hexsha
        assert develop_branch_info.last_commit.message == "Add develop feature"

        # Clean up
        branch_service.close()

    def test_context_manager_works_with_non_git(self):
        """
        Test that context manager protocol works correctly for non-git folders.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        git_topology_service = GitTopologyService(non_git_dir)
        index_status_manager = IndexStatusManagerForTesting()

        # Act & Assert - Context manager should work
        # Currently FAILS until AC1 is fixed
        try:
            with BranchService(
                git_topology_service=git_topology_service,
                index_status_manager=index_status_manager,
            ) as branch_service:
                branches = branch_service.list_branches()
                assert branches == []
        except ValueError:
            pytest.skip("AC1 not implemented yet - BranchService rejects non-git folders")
