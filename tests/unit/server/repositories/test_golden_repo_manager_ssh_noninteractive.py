"""
Tests verifying that GoldenRepoManager git subprocess calls that contact remote
SSH URLs pass the non-interactive SSH environment.

This prevents server worker threads from hanging indefinitely when SSH key
authentication fails — the root cause of Bug: SSH password prompt hangs server.

Strategy: patch subprocess.run and assert that EVERY subprocess.run call whose
command starts with 'git' passes GIT_SSH_COMMAND with BatchMode=yes and
GIT_TERMINAL_PROMPT=0 in the 'env' kwarg.
"""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_GIT_TIMEOUT = 30  # seconds — representative timeout for method-level tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager():
    """Construct a GoldenRepoManager with minimum state for method-level tests."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = object.__new__(GoldenRepoManager)
    resource_config = MagicMock()
    resource_config.git_pull_timeout = 60
    resource_config.git_clone_timeout = 120
    manager.resource_config = resource_config
    return manager


def _make_successful_subprocess_result(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _assert_all_git_calls_have_noninteractive_env(call_args_list, description=""):
    """Assert every subprocess.run call for a git command carries the non-interactive env."""
    git_calls = [
        call
        for call in call_args_list
        if call[0] and isinstance(call[0][0], list) and call[0][0][:1] == ["git"]
    ]
    assert git_calls, f"{description}: expected at least one git subprocess.run call"
    for i, call in enumerate(git_calls):
        env = call[1].get("env")
        assert env is not None, (
            f"{description} call[{i}]: subprocess.run must receive env= kwarg"
        )
        git_ssh = env.get("GIT_SSH_COMMAND", "")
        assert "BatchMode=yes" in git_ssh, (
            f"{description} call[{i}]: GIT_SSH_COMMAND must contain BatchMode=yes, got: {git_ssh!r}"
        )
        assert env.get("GIT_TERMINAL_PROMPT") == "0", (
            f"{description} call[{i}]: GIT_TERMINAL_PROMPT must be '0'"
        )


# ---------------------------------------------------------------------------
# _clone_remote_repository
# ---------------------------------------------------------------------------


def test_clone_remote_repository_all_git_calls_have_noninteractive_env(tmp_path):
    """Every git subprocess.run call in _clone_remote_repository must carry non-interactive env."""
    manager = _make_manager()
    clone_path = str(tmp_path / "clone")

    with patch(
        "subprocess.run", return_value=_make_successful_subprocess_result()
    ) as mock_run:
        manager._clone_remote_repository(
            repo_url="git@github.com:example/repo.git",
            clone_path=clone_path,
            branch=None,
        )

    _assert_all_git_calls_have_noninteractive_env(
        mock_run.call_args_list, "_clone_remote_repository"
    )


# ---------------------------------------------------------------------------
# _validate_repository_accessible
# ---------------------------------------------------------------------------


def test_validate_git_repository_all_git_calls_have_noninteractive_env():
    """Every git subprocess.run call in _validate_git_repository must carry non-interactive env."""
    manager = _make_manager()

    with patch(
        "subprocess.run", return_value=_make_successful_subprocess_result()
    ) as mock_run:
        manager._validate_git_repository("git@gitlab.com:group/repo.git")

    _assert_all_git_calls_have_noninteractive_env(
        mock_run.call_args_list, "_validate_git_repository"
    )


# ---------------------------------------------------------------------------
# _cb_git_fetch_and_validate
# ---------------------------------------------------------------------------


def test_cb_git_fetch_and_validate_all_git_calls_have_noninteractive_env(tmp_path):
    """Every git subprocess.run call in _cb_git_fetch_and_validate must carry non-interactive env."""
    manager = _make_manager()
    branch_result = _make_successful_subprocess_result(stdout="  origin/main\n")

    with patch(
        "subprocess.run",
        side_effect=[
            _make_successful_subprocess_result(),
            branch_result,
        ],
    ) as mock_run:
        manager._cb_git_fetch_and_validate(
            base_clone_path=str(tmp_path),
            target_branch="main",
            git_timeout=TEST_GIT_TIMEOUT,
        )

    _assert_all_git_calls_have_noninteractive_env(
        mock_run.call_args_list, "_cb_git_fetch_and_validate"
    )


# ---------------------------------------------------------------------------
# _cb_checkout_and_pull
# ---------------------------------------------------------------------------


def test_cb_checkout_and_pull_all_git_calls_have_noninteractive_env(tmp_path):
    """Every git subprocess.run call in _cb_checkout_and_pull must carry non-interactive env."""
    manager = _make_manager()

    with patch(
        "subprocess.run", return_value=_make_successful_subprocess_result()
    ) as mock_run:
        manager._cb_checkout_and_pull(
            base_clone_path=str(tmp_path),
            target_branch="main",
            git_timeout=TEST_GIT_TIMEOUT,
        )

    _assert_all_git_calls_have_noninteractive_env(
        mock_run.call_args_list, "_cb_checkout_and_pull"
    )
