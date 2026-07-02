"""
Tests for Bug #699: add_golden_repo silently hardcodes branch="main" causing
git clone --branch main to fail on repos whose default branch is not "main".

Fix: When no branch is specified, omit --branch from git clone entirely so git
uses the remote's HEAD ref (the natural default behavior).
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Named constants — branch names
# ---------------------------------------------------------------------------

BRANCH_MAIN = "main"
BRANCH_MASTER = "master"
BRANCH_DEVELOP = "develop"

# ---------------------------------------------------------------------------
# Named constants — git binary, subcommands, and flags
# ---------------------------------------------------------------------------

_GIT_BIN = "git"
_GIT_INIT_CMD = "init"
_GIT_INIT_BRANCH_FLAG = "--initial-branch"
_GIT_CONFIG_CMD = "config"
_GIT_ADD_CMD = "add"
_GIT_ADD_ALL = "."
_GIT_COMMIT_CMD = "commit"
_GIT_COMMIT_MSG_FLAG = "-m"
_GIT_CLONE_CMD = "clone"
_GIT_BARE_FLAG = "--bare"
_GIT_BRANCH_FLAG = "--branch"
_GIT_REV_PARSE_CMD = "rev-parse"
_GIT_ABBREV_REF_FLAG = "--abbrev-ref"
_GIT_HEAD_REF = "HEAD"

# ---------------------------------------------------------------------------
# Named constants — git config keys/values used in repo setup
# ---------------------------------------------------------------------------

_GIT_USER_EMAIL_KEY = "user.email"
_GIT_USER_EMAIL_VAL = "test@test.com"
_GIT_USER_NAME_KEY = "user.name"
_GIT_USER_NAME_VAL = "Test"

# ---------------------------------------------------------------------------
# Named constants — file/directory names used in repo setup and tests
# ---------------------------------------------------------------------------

_BARE_SOURCE_DIR = "source"
_BARE_REMOTE_DIR = "remote.git"
_INITIAL_README = "README.md"
_INITIAL_README_CONTENT = "test repo"
_INITIAL_COMMIT_MSG = "initial commit"

_MASTER_SETUP_DIR = "master_setup"
_MAIN_SETUP_DIR = "main_setup"
_CLONE_DIR = "clone"
_REPO_DIR = "repo"

# ---------------------------------------------------------------------------
# Named constants — fixture name strings (used in parametrize)
# ---------------------------------------------------------------------------

_MASTER_FIXTURE_NAME = "master_bare_repo"
_MAIN_FIXTURE_NAME = "main_bare_repo"

# ---------------------------------------------------------------------------
# Named constants — parametrize parameter names and error keywords
# ---------------------------------------------------------------------------

_PARAM_NAMES = "fixture_name,explicit_branch,expected_branch"
_CLONE_ERROR_KEYWORDS = ["failed", "error", "branch", "clone"]


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_manager():
    """Create a minimal GoldenRepoManager with enough state to call _clone_remote_repository."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = object.__new__(GoldenRepoManager)
    resource_config = MagicMock()
    resource_config.git_pull_timeout = 300
    manager.resource_config = resource_config
    return manager


def _create_bare_repo(base_path: Path, default_branch: str) -> Path:
    """
    Create a minimal bare git repository with one commit on the given branch.

    Serves as a local "remote" so tests never make network calls.
    Returns the path to the bare repo.
    """
    source = base_path / _BARE_SOURCE_DIR
    source.mkdir(parents=True)

    def git(*args: str) -> None:
        result = subprocess.run(
            [_GIT_BIN] + list(args),
            cwd=str(source),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{_GIT_BIN} {' '.join(args)} failed: {result.stderr}"
        )

    git(_GIT_INIT_CMD, f"{_GIT_INIT_BRANCH_FLAG}={default_branch}")
    git(_GIT_CONFIG_CMD, _GIT_USER_EMAIL_KEY, _GIT_USER_EMAIL_VAL)
    git(_GIT_CONFIG_CMD, _GIT_USER_NAME_KEY, _GIT_USER_NAME_VAL)
    (source / _INITIAL_README).write_text(_INITIAL_README_CONTENT)
    git(_GIT_ADD_CMD, _GIT_ADD_ALL)
    git(_GIT_COMMIT_CMD, _GIT_COMMIT_MSG_FLAG, _INITIAL_COMMIT_MSG)

    bare = base_path / _BARE_REMOTE_DIR
    subprocess.run(
        [_GIT_BIN, _GIT_CLONE_CMD, _GIT_BARE_FLAG, str(source), str(bare)],
        capture_output=True,
        check=True,
    )
    return bare


def _get_checked_out_branch(repo_path: str) -> str:
    """Return the name of the currently checked-out branch in repo_path."""
    result = subprocess.run(
        [_GIT_BIN, _GIT_REV_PARSE_CMD, _GIT_ABBREV_REF_FLAG, _GIT_HEAD_REF],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git rev-parse failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def master_bare_repo(tmp_path):
    """Local bare repo whose default branch is 'master'."""
    return _create_bare_repo(tmp_path / _MASTER_SETUP_DIR, default_branch=BRANCH_MASTER)


@pytest.fixture()
def main_bare_repo(tmp_path):
    """Local bare repo whose default branch is 'main'."""
    return _create_bare_repo(tmp_path / _MAIN_SETUP_DIR, default_branch=BRANCH_MAIN)


# ---------------------------------------------------------------------------
# Fast unit tests — mock subprocess, test command construction only
# ---------------------------------------------------------------------------


def test_clone_command_omits_branch_when_none(tmp_path, master_bare_repo):
    """
    When branch=None, _clone_remote_repository must NOT include --branch in the
    subprocess call. Git's native behavior then uses the remote HEAD ref.
    """
    manager = _make_manager()
    clone_path = str(tmp_path / _REPO_DIR)

    completed = MagicMock()
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed) as mock_run:
        manager._clone_remote_repository(
            repo_url=str(master_bare_repo),
            clone_path=clone_path,
            branch=None,
        )

    cmd = mock_run.call_args[0][0]

    assert _GIT_BRANCH_FLAG not in cmd, (
        f"{_GIT_BRANCH_FLAG} must NOT appear in git clone when branch=None, got: {cmd}"
    )
    assert str(master_bare_repo) in cmd
    assert clone_path in cmd


def test_clone_command_includes_branch_when_specified(tmp_path, master_bare_repo):
    """
    When branch="develop", _clone_remote_repository must include
    --branch develop in the subprocess call.
    """
    manager = _make_manager()
    clone_path = str(tmp_path / _REPO_DIR)

    completed = MagicMock()
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed) as mock_run:
        manager._clone_remote_repository(
            repo_url=str(master_bare_repo),
            clone_path=clone_path,
            branch=BRANCH_DEVELOP,
        )

    cmd = mock_run.call_args[0][0]

    assert _GIT_BRANCH_FLAG in cmd, (
        f"{_GIT_BRANCH_FLAG} must appear when branch is specified, got: {cmd}"
    )
    branch_idx = cmd.index(_GIT_BRANCH_FLAG)
    assert cmd[branch_idx + 1] == BRANCH_DEVELOP, (
        f"Value after {_GIT_BRANCH_FLAG} must be '{BRANCH_DEVELOP}', got: {cmd[branch_idx + 1]}"
    )


# ---------------------------------------------------------------------------
# Behaviour tests — real git clone against local bare repos, no network
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize(
    _PARAM_NAMES,
    [
        # No branch specified: git resolves the remote HEAD
        (_MASTER_FIXTURE_NAME, None, BRANCH_MASTER),
        (_MAIN_FIXTURE_NAME, None, BRANCH_MAIN),
        # Explicit branch matching the default: must still work
        (_MASTER_FIXTURE_NAME, BRANCH_MASTER, BRANCH_MASTER),
        (_MAIN_FIXTURE_NAME, BRANCH_MAIN, BRANCH_MAIN),
    ],
)
def test_clone_succeeds_with_correct_branch(
    request, tmp_path, fixture_name, explicit_branch, expected_branch
):
    """
    Parametrized: cloning with no branch or with the correct explicit branch
    must succeed and check out the expected branch.
    """
    bare = request.getfixturevalue(fixture_name)
    manager = _make_manager()
    clone_path = str(tmp_path / _CLONE_DIR)

    result_path = manager._clone_remote_repository(
        repo_url=str(bare),
        clone_path=clone_path,
        branch=explicit_branch,
    )

    assert result_path == clone_path
    assert _get_checked_out_branch(clone_path) == expected_branch


@pytest.mark.slow
def test_clone_with_wrong_branch_fails(tmp_path, master_bare_repo):
    """
    Regression test for Bug #699: cloning a master-default repo with
    branch="main" must raise GitOperationError because 'main' does not exist.

    This was the exact failure mode: the hardcoded 'main' default in the handler
    caused every clone of a master-default repo to fail with this error.
    """
    from code_indexer.server.repositories.golden_repo_manager import GitOperationError

    manager = _make_manager()
    clone_path = str(tmp_path / _CLONE_DIR)

    with pytest.raises(GitOperationError) as exc_info:
        manager._clone_remote_repository(
            repo_url=str(master_bare_repo),
            clone_path=clone_path,
            branch=BRANCH_MAIN,  # "main" does not exist on a master-default repo
        )

    error_message = str(exc_info.value).lower()
    assert any(keyword in error_message for keyword in _CLONE_ERROR_KEYWORDS), (
        f"Expected a clone-failure error message, got: {exc_info.value}"
    )
