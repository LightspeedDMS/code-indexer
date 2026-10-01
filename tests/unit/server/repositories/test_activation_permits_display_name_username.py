"""
Activation for an account with a display-style username (embedded space,
apostrophe, accented letter) succeeds end to end and lands fully
contained under activated_repos_dir/<username>/<alias>: usernames that
validate_username_path_safe accepts are also accepted by
ActivatedRepoManager's realpath-containment checks (_safe_user_dir /
_safe_user_scoped_path).

Reuses the same fixture pattern as the existing containment-focused
ActivatedRepoManager test suite: real filesystem, real git repo, real
LocalCloneBackend (cp --reflink=auto/-a). Only GoldenRepoManager and
BackgroundJobManager are mocked (heavy, unrelated collaborators).
"""

import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend


@pytest.fixture(autouse=True)
def _initialized_server_schema():
    """_do_activate_repository's committer-email-resolution step reaches
    into the global ConfigService singleton for an unrelated SQLite
    database (server_dir/data/cidx_server.db, holding the ssh_keys
    table) -- nothing to do with username validation, but the schema
    must exist for activation to run at all in this sandboxed
    environment. A real deployed server always has this schema
    initialized already; this fixture reproduces that precondition
    without touching production code."""
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.storage.database_manager import DatabaseSchema

    server_dir = get_config_service().config_manager.server_dir
    data_dir = server_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    DatabaseSchema(str(data_dir / "cidx_server.db")).initialize_database()
    yield


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_source(temp_data_dir):
    golden_path = Path(temp_data_dir) / "golden" / "test-repo"
    golden_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=golden_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=golden_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=golden_path,
        check=True,
        capture_output=True,
    )
    (golden_path / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "add", "."], cwd=golden_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=golden_path,
        check=True,
        capture_output=True,
    )
    return golden_path


@pytest.fixture
def golden_repo_manager_mock(golden_repo_source):
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="master",
        clone_path=str(golden_repo_source),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    mock.golden_repos = {"test-repo": golden_repo}
    mock.get_golden_repo = MagicMock(return_value=golden_repo)
    mock.get_actual_repo_path = MagicMock(return_value=str(golden_repo_source))
    mock.resource_config = None
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


@pytest.fixture
def activated_repo_manager(
    temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=LocalCloneBackend(),
    )


@pytest.mark.parametrize("username", ["john smith", "josé", "o'brien"])
class TestActivationSucceedsForDisplayStyleUsername:
    def test_do_activate_repository_clones_into_contained_directory(
        self, activated_repo_manager, temp_data_dir, username
    ):
        result = activated_repo_manager._do_activate_repository(
            username=username,
            golden_repo_alias="test-repo",
            branch_name="master",
            user_alias="my-repo",
        )

        assert result["success"] is True
        activated_path = Path(temp_data_dir) / "activated-repos" / username / "my-repo"
        assert activated_path.is_dir()
        assert (activated_path / "README.md").exists()
        # Contained: nothing is created outside the per-user directory.
        assert not (Path(temp_data_dir) / "my-repo").exists()

    def test_get_activated_repo_path_returns_contained_path(
        self, activated_repo_manager, temp_data_dir, username
    ):
        path = activated_repo_manager.get_activated_repo_path(username, "my-repo")
        expected = str(Path(temp_data_dir) / "activated-repos" / username / "my-repo")
        assert path == expected
