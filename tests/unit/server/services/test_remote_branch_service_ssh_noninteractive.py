"""
Tests verifying that RemoteBranchService subprocess calls pass the full
non-interactive SSH environment.

The existing code passes only GIT_TERMINAL_PROMPT=0 as an isolated env dict
(no PATH, no BatchMode). That is insufficient: SSH hangs are only partially
prevented, and PATH is dropped from the child process environment.

This file documents and enforces the correct behaviour after the fix.
"""

import os
from unittest.mock import MagicMock, patch

from code_indexer.server.services.remote_branch_service import RemoteBranchService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_successful_run(stdout=""):
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _assert_every_call_has_noninteractive_env(call_args_list, description=""):
    """Assert EVERY subprocess.run call carries the full non-interactive SSH env."""
    assert call_args_list, f"{description}: expected at least one subprocess.run call"
    for i, call in enumerate(call_args_list):
        env = call[1].get("env")
        assert env is not None, (
            f"{description} call[{i}]: subprocess.run must receive env= kwarg"
        )
        assert env.get("PATH") == os.environ.get("PATH"), (
            f"{description} call[{i}]: env must inherit PATH from os.environ"
        )
        git_ssh = env.get("GIT_SSH_COMMAND", "")
        assert "BatchMode=yes" in git_ssh, (
            f"{description} call[{i}]: GIT_SSH_COMMAND must contain BatchMode=yes"
        )
        assert env.get("GIT_TERMINAL_PROMPT") == "0", (
            f"{description} call[{i}]: GIT_TERMINAL_PROMPT must be '0'"
        )


# ---------------------------------------------------------------------------
# fetch_remote_branches
# ---------------------------------------------------------------------------


def test_fetch_remote_branches_all_subprocess_calls_have_noninteractive_env():
    """ALL subprocess.run calls in fetch_remote_branches must carry the full non-interactive env."""
    service = RemoteBranchService(timeout=10)
    branch_output = "abc123\trefs/heads/main\n"
    symref_output = "ref: refs/heads/main\tHEAD\n"

    with patch(
        "subprocess.run",
        side_effect=[
            _make_successful_run(stdout=branch_output),
            _make_successful_run(stdout=symref_output),
        ],
    ) as mock_run:
        service.fetch_remote_branches(clone_url="git@example.com:org/repo.git")

    _assert_every_call_has_noninteractive_env(
        mock_run.call_args_list, "fetch_remote_branches"
    )
