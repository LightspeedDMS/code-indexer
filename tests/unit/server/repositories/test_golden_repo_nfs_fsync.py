"""
Tests for Bug #1010: Git clone/fetch/pull fails on NFS-backed golden-repos with fsync error.

Git 2.47+ enforces core.fsync by default, causing I/O errors on NFS v4 mounts
during pack file writes.  The fix adds -c core.fsync=none as positions 1 and 2
in each write subcommand (clone, fetch, pull), immediately after the git binary.
Read-only subcommands (branch -r, checkout, rev-parse, ls-remote) are unaffected.
"""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_GIT_BIN = "git"
_FSYNC_FLAG = "-c"
_FSYNC_VALUE = "core.fsync=none"
_CLONE_CMD = "clone"
_FETCH_CMD = "fetch"
_PULL_CMD = "pull"
_ORIGIN = "origin"

_FAKE_REPO_URL = "https://example.com/repo.git"
_FAKE_BASE_CLONE_PATH = "/tmp/golden-repos/base-clone"
_FAKE_BRANCH = "main"
_FAKE_GIT_TIMEOUT = 300


# ---------------------------------------------------------------------------
# Helper — minimal GoldenRepoManager instance (same pattern as test_clone_branch_default.py)
# ---------------------------------------------------------------------------


def _make_manager():
    """Create a minimal GoldenRepoManager with enough state to call the private methods."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = object.__new__(GoldenRepoManager)
    resource_config = MagicMock()
    resource_config.git_pull_timeout = _FAKE_GIT_TIMEOUT
    manager.resource_config = resource_config
    return manager


def _make_ok_result(stdout: str = "") -> MagicMock:
    """Return a mock subprocess.CompletedProcess with returncode=0."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# Shared assertion helper — eliminates repeated command-inspection logic
# ---------------------------------------------------------------------------


def _assert_fsync_flag_before_subcommand(cmd: list, subcommand: str) -> None:
    """Assert that cmd follows the pattern: ['git', '-c', 'core.fsync=none', <subcommand>, ...].

    Verifies:
    - cmd[0] is 'git'
    - cmd[1] is '-c'  (immediately after git binary, position 1)
    - cmd[2] is 'core.fsync=none'  (immediately after -c, position 2)
    - the subcommand appears after position 2
    """
    assert cmd[0] == _GIT_BIN, f"cmd[0] must be 'git', got: {cmd[0]}"
    assert cmd[1] == _FSYNC_FLAG, (
        f"cmd[1] must be '-c' (NFS fsync fix for Bug #1010), got: {cmd[1]!r}; full cmd: {cmd}"
    )
    assert cmd[2] == _FSYNC_VALUE, (
        f"cmd[2] must be 'core.fsync=none', got: {cmd[2]!r}; full cmd: {cmd}"
    )
    assert subcommand in cmd, (
        f"'{subcommand}' must appear in the command after position 2, got: {cmd}"
    )
    sub_idx = cmd.index(subcommand)
    assert sub_idx > 2, (
        f"'{subcommand}' must appear after '-c core.fsync=none' (position 2), "
        f"but found at index {sub_idx}; full cmd: {cmd}"
    )


# ---------------------------------------------------------------------------
# Tests — verify -c core.fsync=none is present in each write git command
# ---------------------------------------------------------------------------


def test_clone_includes_fsync_none_flag(tmp_path):
    """
    _clone_remote_repository() must build a command starting with
    ['git', '-c', 'core.fsync=none', 'clone', ...] to suppress NFS fsync
    errors (Bug #1010).
    """
    manager = _make_manager()
    clone_path = str(tmp_path / "repo")

    with patch("subprocess.run", return_value=_make_ok_result()) as mock_run:
        manager._clone_remote_repository(
            repo_url=_FAKE_REPO_URL,
            clone_path=clone_path,
            branch=None,
        )

    cmd = mock_run.call_args[0][0]
    _assert_fsync_flag_before_subcommand(cmd, _CLONE_CMD)


def test_fetch_includes_fsync_none_flag():
    """
    _cb_git_fetch_and_validate() must build a fetch command starting with
    ['git', '-c', 'core.fsync=none', 'fetch', ...] to suppress NFS fsync
    errors (Bug #1010).
    """
    manager = _make_manager()

    # branch -r stdout must contain "origin/main" so branch validation passes
    branch_stdout = f"  {_ORIGIN}/{_FAKE_BRANCH}\n"

    with patch(
        "subprocess.run",
        side_effect=[_make_ok_result(), _make_ok_result(stdout=branch_stdout)],
    ) as mock_run:
        manager._cb_git_fetch_and_validate(
            base_clone_path=_FAKE_BASE_CLONE_PATH,
            target_branch=_FAKE_BRANCH,
            git_timeout=_FAKE_GIT_TIMEOUT,
        )

    # First subprocess.run call is git fetch; second is git branch -r (read-only)
    fetch_cmd = mock_run.call_args_list[0][0][0]
    _assert_fsync_flag_before_subcommand(fetch_cmd, _FETCH_CMD)


def test_pull_includes_fsync_none_flag():
    """
    _cb_checkout_and_pull() must build a pull command starting with
    ['git', '-c', 'core.fsync=none', 'pull', ...] to suppress NFS fsync
    errors (Bug #1010).
    """
    manager = _make_manager()

    with patch(
        "subprocess.run",
        side_effect=[_make_ok_result(), _make_ok_result()],
    ) as mock_run:
        manager._cb_checkout_and_pull(
            base_clone_path=_FAKE_BASE_CLONE_PATH,
            target_branch=_FAKE_BRANCH,
            git_timeout=_FAKE_GIT_TIMEOUT,
        )

    # First subprocess.run call is git checkout (read-only); second is git pull
    pull_cmd = mock_run.call_args_list[1][0][0]
    _assert_fsync_flag_before_subcommand(pull_cmd, _PULL_CMD)
