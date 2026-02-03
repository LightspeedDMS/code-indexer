"""
Pytest fixtures for Git Operations and File CRUD Integration Tests.

Provides shared test fixtures for:
- External repository cloning (git@github.com:LightspeedDMS/VivaGoals-to-pptx.git)
- GitRepoStateManager integration for idempotent tests
- FastAPI test application with authentication bypass
- TestClient for HTTP requests
- Activated repository mocking

All fixtures use REAL git operations - NO Python mocks for git commands.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager

from .git_repo_state_manager import GitRepoState, GitRepoStateManager

logger = logging.getLogger(__name__)

# External test repository (read-write access required for push tests)
EXTERNAL_REPO_URL = "git@github.com:LightspeedDMS/VivaGoals-to-pptx.git"
EXTERNAL_REPO_NAME = "VivaGoals-to-pptx"

# Environment variable to skip SSH-dependent tests in CI
SKIP_SSH_TESTS_ENV = "CIDX_SKIP_SSH_TESTS"


def requires_ssh_access(func):
    """Decorator to skip tests that require SSH access to external repos."""
    return pytest.mark.skipif(
        os.environ.get(SKIP_SSH_TESTS_ENV, "").lower() in ("1", "true", "yes"),
        reason=f"Skipped: {SKIP_SSH_TESTS_ENV} is set (no SSH access in CI)",
    )(func)


# ---------------------------------------------------------------------------
# Module-scoped fixtures (shared across all tests in module for performance)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def external_repo_dir() -> Generator[Path, None, None]:
    """
    Clone external test repository to temporary directory.

    This fixture clones git@github.com:LightspeedDMS/VivaGoals-to-pptx.git
    to a temporary directory for use in integration tests.

    Scope: module (cloned once per test module for performance)

    Yields:
        Path to cloned repository

    Note:
        Requires SSH key access to the repository.
        Set CIDX_SKIP_SSH_TESTS=1 to skip tests requiring this fixture.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="cidx_git_ops_test_"))
    repo_path = temp_dir / EXTERNAL_REPO_NAME

    try:
        logger.info(f"Cloning {EXTERNAL_REPO_URL} to {repo_path}")

        # Clone with depth=50 for faster checkout while retaining some history
        # for git log and history-related tests
        subprocess.run(
            ["git", "clone", "--depth=50", EXTERNAL_REPO_URL, str(repo_path)],
            check=True,
            capture_output=True,
            timeout=120,
        )

        # Configure git user for commits (required for commit tests)
        subprocess.run(
            ["git", "config", "user.name", "CIDX Test User"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "cidx-test@example.com"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        logger.info(f"Successfully cloned repository to {repo_path}")
        yield repo_path

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to clone repository: {e.stderr}")
        pytest.skip(f"Could not clone external repo (SSH access required): {e}")
    except subprocess.TimeoutExpired:
        logger.error("Repository clone timed out")
        pytest.skip("Repository clone timed out (network issue)")
    finally:
        # Cleanup: Remove test directory
        if temp_dir.exists():
            logger.info(f"Cleaning up {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def local_test_repo() -> Generator[Path, None, None]:
    """
    Create a local test repository (no network access required).

    This fixture creates a fresh git repository with a local bare remote
    for testing git operations without requiring SSH access.

    Scope: module (created once per test module)

    Yields:
        Path to local test repository
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="cidx_local_repo_test_"))
    repo_path = temp_dir / "test-repo"
    remote_path = temp_dir / "test-remote.git"

    try:
        # Create main repository
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)

        # Configure git user
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        # Create bare remote
        remote_path.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=remote_path,
            check=True,
            capture_output=True,
        )

        # Link repo to remote
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_path)],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        # Create initial commit
        readme = repo_path / "README.md"
        readme.write_text(
            "# Test Repository\n\nCreated for CIDX git operations testing.\n"
        )

        subprocess.run(
            ["git", "add", "."], cwd=repo_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        # Rename branch to main
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        # Push to remote
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        logger.info(f"Created local test repository at {repo_path}")
        yield repo_path

    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def mock_user():
    """
    Create mock User object for authentication bypass.

    Returns a Mock user with power_user role for write operations.
    """
    user = Mock()
    user.username = "testuser"
    user.role = "power_user"  # Needed for write operations
    user.email = "testuser@example.com"
    return user


@pytest.fixture(scope="module")
def test_app(mock_user):
    """
    Create FastAPI test application with authentication bypass.

    Yields:
        FastAPI application with auth dependency overridden
    """

    def mock_get_current_user_dep():
        return mock_user

    app = create_app()
    app.dependency_overrides[get_current_user] = mock_get_current_user_dep

    yield app

    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def client(test_app) -> TestClient:
    """Create TestClient for making HTTP requests."""
    return TestClient(test_app)


# ---------------------------------------------------------------------------
# Function-scoped fixtures (fresh state for each test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def state_manager(local_test_repo: Path) -> GitRepoStateManager:
    """
    Create GitRepoStateManager for the local test repository.

    Args:
        local_test_repo: Path to local test repository

    Returns:
        Configured GitRepoStateManager instance
    """
    return GitRepoStateManager(local_test_repo)


@pytest.fixture(scope="function")
def captured_state(
    state_manager: GitRepoStateManager,
) -> Generator[GitRepoState, None, None]:
    """
    Capture repository state before test and restore after.

    This fixture provides automatic state capture/restore for idempotent tests.

    Usage:
        def test_something(captured_state, state_manager):
            # Test code here - state will be restored automatically
            pass

    Yields:
        GitRepoState captured before test
    """
    state = state_manager.capture_state()
    yield state
    state_manager.restore_state(state)


@pytest.fixture(scope="function")
def synced_remote_state(
    local_test_repo: Path,
    state_manager: GitRepoStateManager,
) -> Generator[GitRepoState, None, None]:
    """
    Capture repository state and ensure remote is synced before/after test.

    This fixture is essential for remote operation tests (push, pull, fetch).
    It ensures that both the local repository AND the bare remote are restored
    to the same initial state after each test.

    Unlike captured_state, this fixture:
    1. Captures the initial state including remote refs
    2. After the test, force-pushes local state to remote to reset it

    Usage:
        def test_push_something(synced_remote_state, local_test_repo):
            # Test code here - both local and remote will be restored
            pass

    Yields:
        GitRepoState captured before test
    """
    state = state_manager.capture_state()
    yield state

    # First restore local state
    state_manager.restore_state(state)

    # Then force-push to sync the remote back to local state
    # This ensures subsequent tests start with remote matching local
    try:
        subprocess.run(
            ["git", "push", "--force", "origin", "main"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to force-push to reset remote: {e}")
    except subprocess.TimeoutExpired:
        logger.warning("Force-push to reset remote timed out")


@pytest.fixture(scope="function")
def activated_local_repo(local_test_repo: Path) -> Generator[str, None, None]:
    """
    Mock activated repository returning local test repo path.

    This fixture patches ActivatedRepoManager to return the local test
    repository path, enabling tests without golden repository setup.

    Yields:
        Repository alias for use in API calls
    """
    alias = "test-local-repo"

    with patch.object(
        ActivatedRepoManager,
        "get_activated_repo_path",
        return_value=str(local_test_repo),
    ):
        yield alias


@pytest.fixture(scope="function")
def activated_external_repo(external_repo_dir: Path) -> Generator[str, None, None]:
    """
    Mock activated repository returning external repo path.

    This fixture patches ActivatedRepoManager to return the external
    repository path for tests requiring real remote operations.

    Yields:
        Repository alias for use in API calls

    Note:
        Tests using this fixture require SSH access to the external repository.
    """
    alias = "test-external-repo"

    with patch.object(
        ActivatedRepoManager,
        "get_activated_repo_path",
        return_value=str(external_repo_dir),
    ):
        yield alias


@pytest.fixture(scope="function")
def external_state_manager(external_repo_dir: Path) -> GitRepoStateManager:
    """
    Create GitRepoStateManager for the external test repository.

    Args:
        external_repo_dir: Path to cloned external repository

    Returns:
        Configured GitRepoStateManager instance
    """
    return GitRepoStateManager(external_repo_dir)


@pytest.fixture(scope="function")
def external_captured_state(
    external_state_manager: GitRepoStateManager,
) -> Generator[GitRepoState, None, None]:
    """
    Capture external repository state before test and restore after.

    Yields:
        GitRepoState captured before test
    """
    state = external_state_manager.capture_state()
    yield state
    external_state_manager.restore_state(state)


# ---------------------------------------------------------------------------
# Confirmation Token Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def get_confirmation_token(client):
    """
    Factory fixture to obtain confirmation tokens for destructive operations.

    Returns a function that requests a confirmation token from the API.

    Usage:
        def test_hard_reset(get_confirmation_token, activated_local_repo, client):
            # First call without token to get the token
            token = get_confirmation_token(
                client,
                f"/api/v1/repos/{activated_local_repo}/git/reset",
                {"mode": "hard"}
            )
            # Second call with token to execute
            response = client.post(
                f"/api/v1/repos/{activated_local_repo}/git/reset",
                json={"mode": "hard", "confirmation_token": token}
            )
    """

    def _get_token(test_client: TestClient, endpoint: str, payload: dict) -> str:
        """
        Request a confirmation token for a destructive operation.

        Args:
            test_client: TestClient instance
            endpoint: API endpoint URL
            payload: Request payload (without confirmation_token)

        Returns:
            Confirmation token string

        Raises:
            AssertionError: If token not returned in response
        """
        response = test_client.post(endpoint, json=payload)

        # Token requests may return 200 or 202 depending on implementation
        assert response.status_code in (200, 202), (
            f"Expected 200/202 for token request, got {response.status_code}: "
            f"{response.text}"
        )

        data = response.json()
        assert "token" in data, f"No token in response: {data}"
        assert (
            data.get("requires_confirmation") is True
        ), f"Expected requires_confirmation=True: {data}"

        return data["token"]

    return _get_token


# ---------------------------------------------------------------------------
# Test Data Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def test_file_content() -> str:
    """Return standard test file content."""
    return "Test content created by CIDX git operations test suite.\n"


@pytest.fixture(scope="function")
def unique_filename() -> str:
    """Generate a unique filename for test files."""
    import uuid

    return f"test_file_{uuid.uuid4().hex[:8]}.txt"


@pytest.fixture(scope="function")
def unique_branch_name() -> str:
    """Generate a unique branch name for test branches."""
    import uuid

    return f"cidx-test-branch-{uuid.uuid4().hex[:8]}"
