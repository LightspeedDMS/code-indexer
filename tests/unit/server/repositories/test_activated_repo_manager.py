"""
Unit tests for ActivatedRepoManager.

Tests the core functionality of activated repository management including:
- Activating repositories for users
- Listing user's activated repositories
- Deactivating repositories
- Branch management
- Copy-on-write cloning from golden repositories
- Integration with background job system
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
    ActivatedRepo,
    ActivatedRepoError,
    GitOperationError,
)
from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepo


@pytest.mark.e2e
class TestActivatedRepoManager:
    """Test suite for ActivatedRepoManager functionality."""

    @pytest.fixture
    def temp_data_dir(self):
        """Create temporary data directory for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def golden_repo_manager_mock(self):
        """Mock golden repo manager."""
        mock = MagicMock()

        # Mock golden repo data
        golden_repo = GoldenRepo(
            alias="test-repo",
            repo_url="https://github.com/example/test-repo.git",
            default_branch="main",
            clone_path="/path/to/golden/test-repo",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        golden_repos_dict = {"test-repo": golden_repo}
        mock.golden_repos = golden_repos_dict
        mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
        return mock

    @pytest.fixture
    def background_job_manager_mock(self):
        """Mock background job manager."""
        mock = MagicMock()
        mock.submit_job.return_value = "job-123"
        return mock

    @pytest.fixture
    def mock_clone_backend(self):
        """Mock CloneBackend for CoW clone operations."""
        backend = MagicMock()
        backend.create_clone_at_path.return_value = "/dest/path"
        return backend

    @pytest.fixture
    def activated_repo_manager(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        mock_clone_backend,
    ):
        """Create ActivatedRepoManager instance with temp directory."""
        return ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=mock_clone_backend,
        )

    def test_initialization_creates_activated_repos_directory(self, temp_data_dir):
        """Test that ActivatedRepoManager creates activated repos directory on initialization."""
        # Remove the directory to test creation
        activated_dir = os.path.join(temp_data_dir, "activated-repos")
        if os.path.exists(activated_dir):
            shutil.rmtree(activated_dir)

        ActivatedRepoManager(data_dir=temp_data_dir)

        # Check directory is created
        assert os.path.exists(activated_dir)
        assert os.path.isdir(activated_dir)

    def test_initialization_with_default_data_dir(self):
        """Test initialization with default data directory."""
        with patch("pathlib.Path.home") as mock_home:
            import tempfile

            mock_home.return_value = Path(tempfile.gettempdir()) / "test_user"

            with patch("os.makedirs") as mock_makedirs:
                with patch(
                    "src.code_indexer.server.repositories.activated_repo_manager.GoldenRepoManager"
                ):
                    with patch(
                        "src.code_indexer.server.repositories.activated_repo_manager.BackgroundJobManager"
                    ):
                        ActivatedRepoManager()

                        expected_activated_dir = str(
                            Path(tempfile.gettempdir())
                            / "test_user"
                            / ".cidx-server"
                            / "data"
                            / "activated-repos"
                        )
                        mock_makedirs.assert_called_with(
                            expected_activated_dir, exist_ok=True
                        )

    def test_activate_repository_success(
        self, activated_repo_manager, background_job_manager_mock
    ):
        """Test successful repository activation."""
        username = "testuser"
        golden_repo_alias = "test-repo"
        branch_name = "main"
        user_alias = "my-repo"

        # Test activation
        job_id = activated_repo_manager.activate_repository(
            username=username,
            golden_repo_alias=golden_repo_alias,
            branch_name=branch_name,
            user_alias=user_alias,
        )

        # Verify job was submitted
        assert job_id == "job-123"
        background_job_manager_mock.submit_job.assert_called_once()

        # Verify job was submitted with correct parameters
        call_args = background_job_manager_mock.submit_job.call_args
        assert call_args[0][0] == "activate_repository"  # operation_type

    def test_activate_repository_golden_repo_not_found(self, activated_repo_manager):
        """Test activation fails when golden repo doesn't exist."""
        username = "testuser"
        golden_repo_alias = "nonexistent-repo"

        with pytest.raises(
            ActivatedRepoError, match="Golden repository 'nonexistent-repo' not found"
        ):
            activated_repo_manager.activate_repository(
                username=username, golden_repo_alias=golden_repo_alias
            )

    def test_activate_repository_already_activated(
        self, activated_repo_manager, temp_data_dir
    ):
        """Test activation fails when repository already activated for user."""
        username = "testuser"
        golden_repo_alias = "test-repo"
        user_alias = "my-repo"

        # Create user directory and existing activation
        user_dir = os.path.join(temp_data_dir, "activated-repos", username)
        os.makedirs(user_dir, exist_ok=True)

        existing_activation = {
            "user_alias": user_alias,
            "golden_repo_alias": golden_repo_alias,
            "current_branch": "main",
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "last_accessed": datetime.now(timezone.utc).isoformat(),
        }

        metadata_file = os.path.join(user_dir, f"{user_alias}_metadata.json")
        with open(metadata_file, "w") as f:
            json.dump(existing_activation, f)

        # Create corresponding repo directory
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        # Test activation fails
        with pytest.raises(
            ActivatedRepoError, match="Repository 'my-repo' already activated"
        ):
            activated_repo_manager.activate_repository(
                username=username,
                golden_repo_alias=golden_repo_alias,
                user_alias=user_alias,
            )

    def test_list_activated_repositories_empty(self, activated_repo_manager):
        """Test listing activated repositories when none exist."""
        username = "testuser"

        result = activated_repo_manager.list_activated_repositories(username)

        assert result == []

    def test_list_activated_repositories_with_data(
        self, activated_repo_manager, temp_data_dir
    ):
        """Test listing activated repositories with existing data."""
        username = "testuser"

        # Create user directory with activated repos
        user_dir = os.path.join(temp_data_dir, "activated-repos", username)
        os.makedirs(user_dir, exist_ok=True)

        # Create two activated repos
        repo1_data = {
            "user_alias": "repo1",
            "golden_repo_alias": "golden1",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        repo2_data = {
            "user_alias": "repo2",
            "golden_repo_alias": "golden2",
            "current_branch": "develop",
            "activated_at": "2024-01-02T12:00:00Z",
            "last_accessed": "2024-01-02T13:00:00Z",
        }

        # Write metadata files
        with open(os.path.join(user_dir, "repo1_metadata.json"), "w") as f:
            json.dump(repo1_data, f)

        with open(os.path.join(user_dir, "repo2_metadata.json"), "w") as f:
            json.dump(repo2_data, f)

        # Create corresponding directories
        os.makedirs(os.path.join(user_dir, "repo1"))
        os.makedirs(os.path.join(user_dir, "repo2"))

        # Test listing
        result = activated_repo_manager.list_activated_repositories(username)

        assert len(result) == 2
        assert any(repo["user_alias"] == "repo1" for repo in result)
        assert any(repo["user_alias"] == "repo2" for repo in result)

    def test_deactivate_repository_success(
        self, activated_repo_manager, temp_data_dir, background_job_manager_mock
    ):
        """Test successful repository deactivation."""
        username = "testuser"
        user_alias = "repo1"

        # Create user directory with activated repo
        user_dir = os.path.join(temp_data_dir, "activated-repos", username)
        os.makedirs(user_dir, exist_ok=True)

        repo_data = {
            "user_alias": user_alias,
            "golden_repo_alias": "golden1",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        # Write metadata file and create repo directory
        with open(os.path.join(user_dir, f"{user_alias}_metadata.json"), "w") as f:
            json.dump(repo_data, f)
        os.makedirs(os.path.join(user_dir, user_alias))

        # Test deactivation
        job_id = activated_repo_manager.deactivate_repository(username, user_alias)

        # Verify job was submitted
        assert job_id == "job-123"
        background_job_manager_mock.submit_job.assert_called_once()

    def test_deactivate_repository_not_found(self, activated_repo_manager):
        """Test deactivation fails when repository not found."""
        username = "testuser"
        user_alias = "nonexistent"

        with pytest.raises(
            ActivatedRepoError, match="Activated repository 'nonexistent' not found"
        ):
            activated_repo_manager.deactivate_repository(username, user_alias)

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_clone_with_copy_on_write_success_git_repo(
        self, mock_subprocess, mock_exists, activated_repo_manager, mock_clone_backend
    ):
        """Test successful git clone for git repositories via CloneBackend."""
        golden_path = "/path/to/golden/repo"
        activated_path = "/path/to/activated/repo"

        # Mock that source is a git repository
        mock_exists.return_value = True

        # Mock subprocess calls with different responses (git ops after clone)
        def subprocess_side_effect(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""

            # Check command to return appropriate stdout
            if args[0][0] == "git" and args[0][1] == "rev-parse":
                # Return "false" for bare repository check (non-bare repo)
                result.stdout = "false"
            elif (
                args[0][0] == "git"
                and args[0][1] == "remote"
                and args[0][2] == "get-url"
            ):
                # Return GitHub URL for origin remote
                result.stdout = "git@github.com:example/repo.git"
            else:
                result.stdout = ""

            return result

        mock_subprocess.side_effect = subprocess_side_effect

        result = activated_repo_manager._clone_with_copy_on_write(
            golden_path, activated_path
        )

        assert result is True

        # Verify CloneBackend.create_clone_at_path was called (Story #1034 Commit 4)
        mock_clone_backend.create_clone_at_path.assert_called_once_with(
            golden_path,
            activated_path,
            preserve_attrs=False,
            timeout=120,
        )

        # Verify git operations still run after the clone (no cp subprocess for clone)
        cp_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list) and c.args[0][:1] == ["cp"]
        ]
        assert len(cp_calls) == 0, (
            f"subprocess.run cp must not be called when clone_backend is used: {cp_calls}"
        )

        # git rev-parse still called for bare detection
        git_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list) and c.args[0][:1] == ["git"]
        ]
        assert len(git_calls) >= 1, "git operations must still run after clone"

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_clone_with_copy_on_write_success_non_git_repo(
        self, mock_subprocess, mock_exists, activated_repo_manager, mock_clone_backend
    ):
        """Test successful CoW clone for non-git directories via CloneBackend."""
        golden_path = "/path/to/golden/repo"
        activated_path = "/path/to/activated/repo"

        # Mock that source is NOT a git repository
        mock_exists.return_value = False

        # Mock all subprocess calls to return success
        mock_subprocess.return_value.returncode = 0

        result = activated_repo_manager._clone_with_copy_on_write(
            golden_path, activated_path
        )

        assert result is True

        # Verify CloneBackend.create_clone_at_path was called (Story #1034 Commit 4)
        mock_clone_backend.create_clone_at_path.assert_called_once_with(
            golden_path,
            activated_path,
            preserve_attrs=False,
            timeout=120,
        )

        # For non-git repos, no cp subprocess (backend handles the clone)
        cp_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list) and c.args[0][:1] == ["cp"]
        ]
        assert len(cp_calls) == 0, (
            f"subprocess.run cp must not be called when clone_backend is used: {cp_calls}"
        )

    @patch("subprocess.run")
    def test_clone_with_copy_on_write_failure_raises_exception(
        self, mock_subprocess, activated_repo_manager, mock_clone_backend
    ):
        """Test that CloneBackend failure raises ActivatedRepoError (no fallback)."""
        golden_path = "/path/to/golden/repo"
        activated_path = "/path/to/activated/repo"

        # Mock clone_backend raising an exception (simulates CoW failure)
        mock_clone_backend.create_clone_at_path.side_effect = Exception(
            "Copy-on-write not supported"
        )

        # Should raise ActivatedRepoError wrapping the backend error
        with pytest.raises(ActivatedRepoError, match="Clone operation failed"):
            activated_repo_manager._clone_with_copy_on_write(
                golden_path, activated_path
            )

    @patch("subprocess.run")
    def test_switch_branch_success(
        self, mock_subprocess, activated_repo_manager, temp_data_dir
    ):
        """Test successful branch switching with our improved logic."""
        username = "testuser"
        user_alias = "repo1"
        new_branch = "feature-branch"

        # Create user directory with activated repo
        user_dir = os.path.join(temp_data_dir, "activated-repos", username)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        repo_data = {
            "user_alias": user_alias,
            "golden_repo_alias": "golden1",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        with open(os.path.join(user_dir, f"{user_alias}_metadata.json"), "w") as f:
            json.dump(repo_data, f)

        # Mock git operations for our improved branch switching logic
        def mock_subprocess_side_effect(cmd, **kwargs):
            mock_result = MagicMock()
            if cmd == ["git", "remote", "get-url", "origin"]:
                # Mock remote URL check - return a remote URL to trigger fetch attempt
                mock_result.returncode = 0
                mock_result.stdout = "https://github.com/test/repo.git"
                return mock_result
            elif cmd == ["git", "fetch", "origin"]:
                # Mock successful fetch
                mock_result.returncode = 0
                return mock_result
            elif cmd == ["git", "checkout", "-B", new_branch, f"origin/{new_branch}"]:
                # Mock successful remote branch checkout
                mock_result.returncode = 0
                return mock_result
            else:
                # Default success for other commands
                mock_result.returncode = 0
                return mock_result

        mock_subprocess.side_effect = mock_subprocess_side_effect

        result = activated_repo_manager.switch_branch(username, user_alias, new_branch)

        assert result["success"] is True
        assert new_branch in result["message"]

        # With our new logic, successful remote fetch should indicate remote sync
        assert "remote sync" in result["message"] or "local branch" in result["message"]

    @patch("subprocess.run")
    def test_switch_branch_git_operation_fails(
        self, mock_subprocess, activated_repo_manager, temp_data_dir
    ):
        """Test branch switching fails when branch doesn't exist in any form."""
        username = "testuser"
        user_alias = "repo1"
        new_branch = "nonexistent-branch"

        # Create user directory with activated repo
        user_dir = os.path.join(temp_data_dir, "activated-repos", username)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        repo_data = {
            "user_alias": user_alias,
            "golden_repo_alias": "golden1",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        with open(os.path.join(user_dir, f"{user_alias}_metadata.json"), "w") as f:
            json.dump(repo_data, f)

        # Mock git operations that simulate branch not existing anywhere
        def mock_subprocess_side_effect(cmd, **kwargs):
            mock_result = MagicMock()
            if cmd == ["git", "remote", "get-url", "origin"]:
                # Mock remote URL check
                mock_result.returncode = 0
                mock_result.stdout = "https://github.com/test/repo.git"
                return mock_result
            elif cmd == ["git", "fetch", "origin"]:
                # Mock successful fetch
                mock_result.returncode = 0
                return mock_result
            elif cmd == ["git", "checkout", "-B", new_branch, f"origin/{new_branch}"]:
                # Mock remote branch checkout failure (branch doesn't exist on remote)
                mock_result.returncode = 1
                mock_result.stderr = (
                    "error: pathspec 'nonexistent-branch' did not match"
                )
                return mock_result
            elif cmd == ["git", "checkout", new_branch]:
                # Mock local branch checkout failure (branch doesn't exist locally)
                mock_result.returncode = 1
                mock_result.stderr = (
                    "error: pathspec 'nonexistent-branch' did not match"
                )
                return mock_result
            elif cmd == [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{new_branch}",
            ]:
                # Mock: no origin branch exists locally
                mock_result.returncode = 1
                return mock_result
            elif cmd == ["git", "show-ref", new_branch]:
                # Mock: branch doesn't exist in any form
                mock_result.returncode = 1
                return mock_result
            else:
                # Default success for other commands
                mock_result.returncode = 0
                return mock_result

        mock_subprocess.side_effect = mock_subprocess_side_effect

        # Our improved error message is more specific
        with pytest.raises(GitOperationError, match="not found in repository"):
            activated_repo_manager.switch_branch(username, user_alias, new_branch)

    def test_branch_name_validation_valid_names(self, activated_repo_manager):
        """Test that valid branch names pass validation."""
        valid_names = [
            "main",
            "feature-branch",
            "feature/new-feature",
            "bugfix_123",
            "release-2.1.0",
            "hotfix/urgent-fix",
            "develop",
            "feature.with.dots",
        ]

        for branch_name in valid_names:
            # Should not raise any exception
            activated_repo_manager._validate_branch_name(branch_name)

    def test_branch_name_validation_invalid_names(self, activated_repo_manager):
        """Test that invalid branch names raise GitOperationError."""
        invalid_names = [
            "",  # empty string
            None,  # None value
            "branch with spaces",  # spaces not allowed
            "branch@symbol",  # @ not allowed
            "branch$money",  # $ not allowed
            "-starts-with-dash",  # cannot start with dash
            "ends.lock",  # cannot end with .lock
            "has..double.dots",  # cannot contain ..
            "branch;injection",  # semicolon not allowed
            "branch|pipe",  # pipe not allowed
        ]

        for branch_name in invalid_names:
            with pytest.raises(GitOperationError):
                activated_repo_manager._validate_branch_name(branch_name)

    def test_get_activated_repo_path(self, activated_repo_manager, temp_data_dir):
        """Test getting activated repository path."""
        username = "testuser"
        user_alias = "repo1"

        expected_path = os.path.join(
            temp_data_dir, "activated-repos", username, user_alias
        )
        actual_path = activated_repo_manager.get_activated_repo_path(
            username, user_alias
        )

        assert actual_path == expected_path

    def test_activated_repo_model_to_dict(self):
        """Test ActivatedRepo model to_dict method."""
        repo = ActivatedRepo(
            user_alias="test-repo",
            golden_repo_alias="golden-test",
            current_branch="main",
            activated_at="2024-01-01T12:00:00Z",
            last_accessed="2024-01-01T13:00:00Z",
        )

        result = repo.to_dict()

        expected = {
            "user_alias": "test-repo",
            "golden_repo_alias": "golden-test",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        assert result == expected

    def test_list_all_activated_repositories_empty(self, activated_repo_manager):
        """Test list_all_activated_repositories when no repositories exist."""
        result = activated_repo_manager.list_all_activated_repositories()
        assert result == []

    def test_list_all_activated_repositories_single_user(
        self, activated_repo_manager, temp_data_dir
    ):
        """Test list_all_activated_repositories with repos from single user."""
        username = "user1"

        # Create user directory with activated repos
        user_dir = os.path.join(temp_data_dir, "activated-repos", username)
        os.makedirs(user_dir, exist_ok=True)

        repo1_data = {
            "user_alias": "repo1",
            "golden_repo_alias": "golden1",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        # Write metadata file and create directory
        with open(os.path.join(user_dir, "repo1_metadata.json"), "w") as f:
            json.dump(repo1_data, f)
        os.makedirs(os.path.join(user_dir, "repo1"))

        # Test listing all repos
        result = activated_repo_manager.list_all_activated_repositories()

        assert len(result) == 1
        assert result[0]["user_alias"] == "repo1"
        assert result[0]["golden_repo_alias"] == "golden1"

    def test_list_all_activated_repositories_multiple_users(
        self, activated_repo_manager, temp_data_dir
    ):
        """Test list_all_activated_repositories returns repos from all users."""
        # Create repos for user1
        user1_dir = os.path.join(temp_data_dir, "activated-repos", "user1")
        os.makedirs(user1_dir, exist_ok=True)

        user1_repo1 = {
            "user_alias": "user1-repo1",
            "golden_repo_alias": "golden1",
            "current_branch": "main",
            "activated_at": "2024-01-01T12:00:00Z",
            "last_accessed": "2024-01-01T13:00:00Z",
        }

        user1_repo2 = {
            "user_alias": "user1-repo2",
            "golden_repo_alias": "golden2",
            "current_branch": "develop",
            "activated_at": "2024-01-02T12:00:00Z",
            "last_accessed": "2024-01-02T13:00:00Z",
        }

        with open(os.path.join(user1_dir, "user1-repo1_metadata.json"), "w") as f:
            json.dump(user1_repo1, f)
        os.makedirs(os.path.join(user1_dir, "user1-repo1"))

        with open(os.path.join(user1_dir, "user1-repo2_metadata.json"), "w") as f:
            json.dump(user1_repo2, f)
        os.makedirs(os.path.join(user1_dir, "user1-repo2"))

        # Create repos for user2
        user2_dir = os.path.join(temp_data_dir, "activated-repos", "user2")
        os.makedirs(user2_dir, exist_ok=True)

        user2_repo1 = {
            "user_alias": "user2-repo1",
            "golden_repo_alias": "golden3",
            "current_branch": "main",
            "activated_at": "2024-01-03T12:00:00Z",
            "last_accessed": "2024-01-03T13:00:00Z",
        }

        with open(os.path.join(user2_dir, "user2-repo1_metadata.json"), "w") as f:
            json.dump(user2_repo1, f)
        os.makedirs(os.path.join(user2_dir, "user2-repo1"))

        # Test listing all repos across all users
        result = activated_repo_manager.list_all_activated_repositories()

        # Should return 3 repos total from both users
        assert len(result) == 3

        # Verify all repos are present
        user_aliases = [repo["user_alias"] for repo in result]
        assert "user1-repo1" in user_aliases
        assert "user1-repo2" in user_aliases
        assert "user2-repo1" in user_aliases


# ---------------------------------------------------------------------------
# Story #1034 Commit 4: _clone_with_copy_on_write routes via CloneBackend
# ---------------------------------------------------------------------------


class TestCloneWithCopyOnWriteUsesBackend:
    """
    Story #1034 Commit 4: _clone_with_copy_on_write must delegate the
    filesystem clone to CloneBackend.create_clone_at_path when clone_backend
    is injected, instead of calling subprocess.run directly for the cp step.
    """

    @pytest.fixture
    def temp_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def mock_clone_backend(self):
        backend = MagicMock()
        backend.create_clone_at_path.return_value = "/dest/path"
        return backend

    @pytest.fixture
    def golden_repo_manager_mock(self):
        mock = MagicMock()
        golden_repo = GoldenRepo(
            alias="test-repo",
            repo_url="https://github.com/example/test-repo.git",
            default_branch="main",
            clone_path="/path/to/golden/test-repo",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        golden_repos_dict = {"test-repo": golden_repo}
        mock.golden_repos = golden_repos_dict
        mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
        return mock

    @pytest.fixture
    def background_job_manager_mock(self):
        mock = MagicMock()
        mock.submit_job.return_value = "job-456"
        return mock

    @pytest.fixture
    def manager_with_backend(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        mock_clone_backend,
    ):
        return ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=mock_clone_backend,
        )

    def test_clone_uses_clone_backend_create_clone_at_path(
        self,
        manager_with_backend,
        mock_clone_backend,
        temp_data_dir,
    ):
        """
        Story #1034 Commit 4: when clone_backend is injected, _clone_with_copy_on_write
        must call clone_backend.create_clone_at_path(source_path, dest_path,
        preserve_attrs=False, timeout=<timeout>) instead of subprocess.run for cp.
        """
        source_path = temp_data_dir + "/source-repo"
        dest_path = temp_data_dir + "/dest-repo"
        os.makedirs(source_path, exist_ok=True)

        with patch("subprocess.run") as mock_subprocess:
            manager_with_backend._clone_with_copy_on_write(source_path, dest_path)

        mock_clone_backend.create_clone_at_path.assert_called_once_with(
            source_path,
            dest_path,
            preserve_attrs=False,
            timeout=120,
        )
        # Direct subprocess.run for cp must NOT be called (backend handles it)
        cp_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list) and c.args[0][:1] == ["cp"]
        ]
        assert len(cp_calls) == 0, (
            f"subprocess.run cp must not be called when clone_backend is used, got: {cp_calls}"
        )

    def test_clone_without_backend_raises_runtime_error(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
    ):
        """
        Story #1034 Commit 4: when clone_backend is None, _clone_with_copy_on_write
        raises RuntimeError (wiring bug guard) wrapped as ActivatedRepoError.
        """
        manager_no_backend = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=None,
        )

        source_path = temp_data_dir + "/source-repo"
        dest_path = temp_data_dir + "/dest-repo"
        os.makedirs(source_path, exist_ok=True)

        with pytest.raises(ActivatedRepoError):
            manager_no_backend._clone_with_copy_on_write(source_path, dest_path)
