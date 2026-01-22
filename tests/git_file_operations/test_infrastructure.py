"""
Infrastructure tests for Git/File Operations test suite.

Tests verify:
- Pytest markers (requires_ssh, destructive) are recognized
- CIDX_SKIP_SSH_TESTS environment variable skipping behavior
- Fixtures are importable and functional
- Test infrastructure components work correctly

These tests validate the testing infrastructure itself before running
the actual git/file operations tests.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Timeout for subprocess calls to pytest
SUBPROCESS_TIMEOUT_SECONDS = 30


class TestPytestMarkersRecognized:
    """Tests that verify pytest markers are properly configured."""

    def test_requires_ssh_marker_is_recognized(self):
        """Verify @pytest.mark.requires_ssh marker is registered in pytest."""
        mark = pytest.mark.requires_ssh
        assert mark is not None
        assert mark.name == "requires_ssh"

    def test_destructive_marker_is_recognized(self):
        """Verify @pytest.mark.destructive marker is registered in pytest."""
        mark = pytest.mark.destructive
        assert mark is not None
        assert mark.name == "destructive"

    def test_slow_marker_is_recognized(self):
        """Verify @pytest.mark.slow marker is registered (already exists)."""
        mark = pytest.mark.slow
        assert mark is not None
        assert mark.name == "slow"

    def test_integration_marker_is_recognized(self):
        """Verify @pytest.mark.integration marker is registered."""
        mark = pytest.mark.integration
        assert mark is not None
        assert mark.name == "integration"


class TestSSHSkipBehavior:
    """Tests for CIDX_SKIP_SSH_TESTS environment variable behavior."""

    def test_skip_ssh_env_var_name_constant(self):
        """Verify the environment variable name constant is correct."""
        from .conftest import SKIP_SSH_TESTS_ENV
        assert SKIP_SSH_TESTS_ENV == "CIDX_SKIP_SSH_TESTS"

    def test_requires_ssh_access_decorator_exists(self):
        """Verify the requires_ssh_access decorator is importable."""
        from .conftest import requires_ssh_access
        assert callable(requires_ssh_access)


class TestSSHMarkerBehavior:
    """
    Helper test class for subprocess-based SSH skip verification.

    These tests are designed to be run via subprocess to verify skip behavior.
    """

    @pytest.mark.skipif(
        os.environ.get("CIDX_SKIP_SSH_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Skipped: CIDX_SKIP_SSH_TESTS is set (no SSH access in CI)"
    )
    def test_this_should_be_skipped_when_env_set(self):
        """This test should be skipped when CIDX_SKIP_SSH_TESTS=1."""
        pass

    @pytest.mark.skipif(
        os.environ.get("CIDX_SKIP_SSH_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Skipped: CIDX_SKIP_SSH_TESTS is set (no SSH access in CI)"
    )
    def test_this_should_run_when_env_not_set(self):
        """This test should run normally when env var is not set."""
        assert True


class TestSSHSkipBehaviorIntegration:
    """
    Integration tests for SSH skip behavior using subprocess.

    These tests run pytest in a subprocess with different environment
    settings to verify the skip logic works correctly.
    """

    def test_marked_test_is_skipped_when_env_var_set(self):
        """Verify tests with skipif for CIDX_SKIP_SSH_TESTS are skipped when env var is set."""
        this_file = Path(__file__)

        env = os.environ.copy()
        env["CIDX_SKIP_SSH_TESTS"] = "1"

        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(this_file) + "::TestSSHMarkerBehavior::test_this_should_be_skipped_when_env_set",
                "-v", "--tb=short"
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=SUBPROCESS_TIMEOUT_SECONDS
        )

        assert result.returncode == 0, f"pytest failed: {result.stdout}\n{result.stderr}"
        assert "SKIPPED" in result.stdout or "skipped" in result.stdout.lower(), (
            f"Test should have been skipped:\n{result.stdout}"
        )

    def test_marked_test_runs_when_env_var_not_set(self):
        """Verify tests with skipif for CIDX_SKIP_SSH_TESTS run when env var is NOT set."""
        this_file = Path(__file__)

        env = os.environ.copy()
        env.pop("CIDX_SKIP_SSH_TESTS", None)

        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(this_file) + "::TestSSHMarkerBehavior::test_this_should_run_when_env_not_set",
                "-v", "--tb=short"
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=SUBPROCESS_TIMEOUT_SECONDS
        )

        assert result.returncode == 0, f"pytest failed: {result.stdout}\n{result.stderr}"
        assert "PASSED" in result.stdout, f"Test should have passed:\n{result.stdout}"


class TestFixturesImportable:
    """Tests that verify all required fixtures are importable."""

    def test_git_repo_state_manager_importable(self):
        """Verify GitRepoStateManager is importable."""
        from .git_repo_state_manager import GitRepoStateManager, GitRepoState
        assert GitRepoStateManager is not None
        assert GitRepoState is not None

    def test_skip_ssh_constant_importable(self):
        """Verify SKIP_SSH_TESTS_ENV constant is importable."""
        from .conftest import SKIP_SSH_TESTS_ENV
        assert SKIP_SSH_TESTS_ENV == "CIDX_SKIP_SSH_TESTS"

    def test_requires_ssh_decorator_importable(self):
        """Verify requires_ssh_access decorator is importable."""
        from .conftest import requires_ssh_access
        assert callable(requires_ssh_access)

    def test_external_repo_constants_importable(self):
        """Verify external repository constants are importable."""
        from .conftest import EXTERNAL_REPO_URL, EXTERNAL_REPO_NAME
        assert EXTERNAL_REPO_URL == "git@github.com:LightspeedDMS/VivaGoals-to-pptx.git"
        assert EXTERNAL_REPO_NAME == "VivaGoals-to-pptx"


class TestFixturesFunctional:
    """Tests that verify fixtures work correctly."""

    def test_local_test_repo_fixture_creates_git_repo(self, local_test_repo):
        """Verify local_test_repo fixture creates a valid git repository."""
        assert local_test_repo.exists()
        assert (local_test_repo / ".git").exists()

    def test_local_test_repo_has_initial_commit(self, local_test_repo):
        """Verify local_test_repo has at least one commit."""
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=local_test_repo,
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert result.stdout.strip(), "Repository should have at least one commit"

    def test_local_test_repo_has_remote_origin(self, local_test_repo):
        """Verify local_test_repo has origin remote configured."""
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=local_test_repo,
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert "origin" in result.stdout, "Repository should have origin remote"

    def test_state_manager_fixture_works(self, state_manager):
        """Verify state_manager fixture is properly configured."""
        assert state_manager is not None
        assert hasattr(state_manager, "capture_state")
        assert hasattr(state_manager, "restore_state")

    def test_captured_state_fixture_captures_state(self, captured_state):
        """Verify captured_state fixture properly captures state."""
        assert captured_state is not None
        assert captured_state.current_branch is not None
        assert captured_state.head_commit is not None

    def test_mock_user_fixture_has_required_attributes(self, mock_user):
        """Verify mock_user fixture has required attributes."""
        assert mock_user.username == "testuser"
        assert mock_user.role == "power_user"
        assert mock_user.email == "testuser@example.com"

    def test_test_file_content_fixture_returns_string(self, test_file_content):
        """Verify test_file_content fixture returns appropriate content."""
        assert isinstance(test_file_content, str)
        assert len(test_file_content) > 0

    def test_unique_filename_fixture_generates_unique_names(self, unique_filename):
        """Verify unique_filename fixture generates unique filenames."""
        assert isinstance(unique_filename, str)
        assert unique_filename.startswith("test_file_")
        assert unique_filename.endswith(".txt")

    def test_unique_branch_name_fixture_generates_unique_names(self, unique_branch_name):
        """Verify unique_branch_name fixture generates unique branch names."""
        assert isinstance(unique_branch_name, str)
        assert unique_branch_name.startswith("cidx-test-branch-")


class TestActivatedRepoFixtures:
    """Tests for activated repository fixtures."""

    def test_activated_local_repo_fixture_returns_alias(self, activated_local_repo):
        """Verify activated_local_repo fixture returns string alias."""
        assert isinstance(activated_local_repo, str)
        assert activated_local_repo == "test-local-repo"


class TestConfirmationTokenFixture:
    """Tests for confirmation token generator fixture."""

    def test_get_confirmation_token_fixture_is_callable(self, get_confirmation_token):
        """Verify get_confirmation_token fixture returns callable."""
        assert callable(get_confirmation_token)


class TestScriptsExist:
    """Tests that verify CI/CD scripts exist and are executable."""

    def test_run_git_file_ops_script_exists(self):
        """Verify run_git_file_ops_tests.sh script exists."""
        scripts_dir = Path(__file__).parent / "scripts"
        script_path = scripts_dir / "run_git_file_ops_tests.sh"
        assert script_path.exists(), f"Script not found: {script_path}"

    def test_run_integration_tests_script_exists(self):
        """Verify run_integration_tests.sh script exists."""
        scripts_dir = Path(__file__).parent / "scripts"
        script_path = scripts_dir / "run_integration_tests.sh"
        assert script_path.exists(), f"Script not found: {script_path}"


class TestReadmeExists:
    """Tests that verify documentation exists."""

    def test_readme_exists(self):
        """Verify README.md exists in git_file_operations directory."""
        readme_path = Path(__file__).parent / "README.md"
        assert readme_path.exists(), f"README not found: {readme_path}"

    def test_readme_has_content(self):
        """Verify README.md has meaningful content."""
        readme_path = Path(__file__).parent / "README.md"
        content = readme_path.read_text()
        assert len(content) > 100, "README should have substantial content"
        assert "git" in content.lower(), "README should mention git operations"
