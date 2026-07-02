"""
Tests for git_subprocess_env helper module.

Verifies that build_non_interactive_git_env returns an environment dict
that forces SSH into non-interactive, fail-fast mode to prevent server
worker threads from hanging when SSH key authentication fails.
"""

import os

import pytest

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env


@pytest.mark.parametrize(
    "expected_option",
    [
        "BatchMode=yes",
        "ConnectTimeout=",
        "StrictHostKeyChecking=accept-new",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "PubkeyAuthentication=yes",
    ],
)
def test_ssh_command_contains_required_option(expected_option):
    """GIT_SSH_COMMAND must contain each required SSH non-interactive option."""
    result = build_non_interactive_git_env()
    assert "GIT_SSH_COMMAND" in result
    assert expected_option in result["GIT_SSH_COMMAND"]


def test_git_terminal_prompt_disabled():
    """GIT_TERMINAL_PROMPT=0 disables git's own HTTP credential prompt."""
    result = build_non_interactive_git_env()
    assert result["GIT_TERMINAL_PROMPT"] == "0"


def test_env_inherits_calling_process_vars(monkeypatch):
    """PATH, HOME and arbitrary env vars from os.environ must be preserved."""
    monkeypatch.setenv("PATH", "/custom/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/test/home/dir")
    monkeypatch.setenv("CIDX_TEST_MARKER_XYZ", "sentinel_value_123")
    result = build_non_interactive_git_env()
    assert result["PATH"] == "/custom/bin:/usr/bin"
    assert result["HOME"] == "/test/home/dir"
    assert result["CIDX_TEST_MARKER_XYZ"] == "sentinel_value_123"


def test_does_not_mutate_os_environ():
    """build_non_interactive_git_env must not modify os.environ."""
    snapshot = dict(os.environ)
    result = build_non_interactive_git_env()
    result["CIDX_MUTATION_SENTINEL"] = "mutated"
    assert dict(os.environ) == snapshot
    assert "CIDX_MUTATION_SENTINEL" not in os.environ


def test_returns_new_dict_each_call():
    """Each invocation must return a distinct dict (no shared mutable state)."""
    env1 = build_non_interactive_git_env()
    env2 = build_non_interactive_git_env()
    assert env1 is not env2
