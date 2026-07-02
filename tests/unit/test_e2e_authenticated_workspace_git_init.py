"""
TDD tests for Bug 4: _init_git_workspace helper extracted from the
authenticated_workspace fixture in tests/e2e/cli_remote/conftest.py.

Red phase: these tests FAIL before _init_git_workspace is implemented.

Coverage:
  - .git directory: workspace contains a .git directory after _init_git_workspace
  - remote origin:  git config --get remote.origin.url returns the provided URL
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from tests.e2e.cli_remote.conftest import _init_git_workspace
from tests.e2e.helpers import GIT_SUBPROCESS_TIMEOUT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace_with_seed_url(tmp_path: Path) -> Tuple[Path, str]:
    """Return a (workspace, seed_url) pair for git-init tests.

    ``workspace`` is a freshly created directory.
    ``seed_url`` is a filesystem path to a minimal git repo with one commit,
    mirroring how the E2E suite builds ``str(e2e_config.seed_cache_dir / 'markupsafe')``.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    seed_dir = tmp_path / "seed" / "markupsafe"
    seed_dir.mkdir(parents=True)

    # Create a minimal git repo with one commit so git clone works
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(seed_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(seed_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(seed_dir),
        check=True,
        capture_output=True,
    )
    (seed_dir / "README.md").write_text("seed repo")
    subprocess.run(
        ["git", "add", "."], cwd=str(seed_dir), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(seed_dir),
        check=True,
        capture_output=True,
    )

    seed_url = str(seed_dir)
    return workspace, seed_url


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _git_config_get(key: str, cwd: Path) -> str:
    """Return the value of a git config key in ``cwd``.

    Raises ``subprocess.CalledProcessError`` on non-zero exit so callers
    see a real failure rather than a silent empty-string default.
    """
    result = subprocess.run(
        ["git", "config", "--get", key],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitGitWorkspace:
    """_init_git_workspace must create a git repo with a remote origin.

    ``cidx query`` in remote mode calls:
      1. ``GitTopologyService.is_git_available()`` -- requires a .git directory
      2. ``git config --get remote.origin.url``    -- requires remote.origin.url
    Without both, repository linking fails and the query command exits non-zero.
    """

    def test_git_directory_created(
        self,
        workspace_with_seed_url: Tuple[Path, str],
    ) -> None:
        """_init_git_workspace creates a .git directory in the workspace.

        Without this directory, GitTopologyService.is_git_available() returns
        False and cidx query raises RepositoryLinkingError before sending
        any request to the server.
        """
        workspace, seed_url = workspace_with_seed_url

        _init_git_workspace(workspace, remote_url=seed_url)

        assert (workspace / ".git").is_dir(), (
            "Expected .git directory in workspace after _init_git_workspace, "
            "but it was absent."
        )

    def test_remote_origin_url_matches_provided_url(
        self,
        workspace_with_seed_url: Tuple[Path, str],
    ) -> None:
        """_init_git_workspace sets remote.origin.url to the provided URL.

        ``cidx query`` reads this URL via ``git config --get remote.origin.url``
        and passes it to the server's repository-discovery API.  The URL must
        match the one used when registering the golden repo
        (``str(e2e_config.seed_cache_dir / 'markupsafe')``).
        """
        workspace, seed_url = workspace_with_seed_url

        _init_git_workspace(workspace, remote_url=seed_url)

        actual_url = _git_config_get("remote.origin.url", workspace)
        assert actual_url == seed_url, (
            f"Expected remote.origin.url == {seed_url!r} but got "
            f"{actual_url!r}.  _init_git_workspace must run "
            "'git remote add origin <remote_url>' so that repository linking "
            "matches the registered golden repo."
        )
