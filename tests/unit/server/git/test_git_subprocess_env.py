"""
Tests for git_subprocess_env helper module.

Verifies that build_non_interactive_git_env returns an environment dict
that forces SSH into non-interactive, fail-fast mode to prevent server
worker threads from hanging when SSH key authentication fails.
"""

import os
import subprocess
from pathlib import Path

import pytest

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

REBASE_TIMEOUT_SECONDS = 15


def _run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _create_repo_with_diverging_branches(repo: Path) -> None:
    """Create master + feature branches that both edit the same line."""
    _run(repo, "init", "-b", "master")
    _run(repo, "config", "user.email", "test@test.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("line1\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "init")

    _run(repo, "checkout", "-b", "feature")
    (repo / "f.txt").write_text("line1\nfeature line\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "feature change")

    _run(repo, "checkout", "master")
    (repo / "f.txt").write_text("line1\nmaster line\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "master change")

    _run(repo, "checkout", "feature")


def _start_rebase_and_assert_conflict(repo: Path) -> None:
    """Start a rebase that MUST conflict; assert it actually stopped there.

    core.editor=true is scoped to this one setup step only -- it never
    actually needs an editor here since the rebase stops at the conflict
    before any commit is finalized -- and is explicitly NOT present on the
    `rebase --continue` call the test makes afterward, which is the real
    assertion: that build_non_interactive_git_env()'s own output alone is
    sufficient.
    """
    setup_result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.editor=true", "rebase", "master"],
        capture_output=True,
        text=True,
    )
    assert setup_result.returncode != 0, (
        "Expected the rebase setup step to stop on a genuine conflict, but "
        f"it exited 0: stdout={setup_result.stdout!r}"
    )
    assert (repo / ".git" / "rebase-merge").exists() or (
        repo / ".git" / "rebase-apply"
    ).exists(), "Expected repo to be left in an in-progress rebase/conflict state"


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


def test_git_editor_set_to_noninteractive_value(monkeypatch):
    """GIT_EDITOR and GIT_SEQUENCE_EDITOR must resolve to a non-interactive no-op.

    Without these, a git operation that needs to write an automatic commit
    message (e.g. a non-fast-forward merge, or `rebase --continue`) will try
    to invoke an interactive editor in a server process that has no terminal
    -- hanging or failing with "Terminal is dumb, but EDITOR unset" (Bug #1578).
    """
    monkeypatch.delenv("GIT_EDITOR", raising=False)
    monkeypatch.delenv("GIT_SEQUENCE_EDITOR", raising=False)
    result = build_non_interactive_git_env()
    assert result["GIT_EDITOR"] == "true"
    assert result["GIT_SEQUENCE_EDITOR"] == "true"


def test_git_editor_caller_override_preserved(monkeypatch):
    """A caller-supplied GIT_EDITOR/GIT_SEQUENCE_EDITOR must not be clobbered.

    build_non_interactive_git_env() must use setdefault (not a hard
    overwrite) so a caller that deliberately configured its own editor via
    an inherited/merged env is not silently overridden.
    """
    monkeypatch.setenv("GIT_EDITOR", "custom-script")
    monkeypatch.setenv("GIT_SEQUENCE_EDITOR", "custom-sequence-script")
    result = build_non_interactive_git_env()
    assert result["GIT_EDITOR"] == "custom-script"
    assert result["GIT_SEQUENCE_EDITOR"] == "custom-sequence-script"


def test_env_prevents_terminal_dumb_editor_failure_on_commit_finalization(
    tmp_path: Path, monkeypatch
):
    """Real regression test for Bug #1578's actual reproducible failure class.

    Empirical investigation (see issue #1578 discussion) established that
    `git merge`/`git pull --no-ff` only invoke an interactive editor when
    BOTH stdin and stdout are real ttys -- a condition run_git_command's
    capture_output=True (which always pipes stdout) structurally prevents.
    But `git rebase --continue` (finalizing a commit after a resolved
    conflict) has no such isatty gate: it fails immediately with "Terminal
    is dumb, but EDITOR unset" whenever TERM is dumb/unset and no editor is
    configured, REGARDLESS of tty state. This is the exact failure class the
    now-deleted CidxMetaBackupSync._git() narrow fix (commit 5537424e)
    protected against, and the exact class build_non_interactive_git_env()
    must protect every current and future call site against.
    """
    # The calling shell/session may already export GIT_EDITOR (or EDITOR),
    # which would silently mask this test's RED signal by making
    # build_non_interactive_git_env()'s dict(os.environ) inherit a working
    # editor before the fix even applies its own default. Strip them so this
    # test genuinely proves the fix, not an ambient environment accident.
    for var in ("GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "EDITOR", "VISUAL"):
        monkeypatch.delenv(var, raising=False)

    repo = tmp_path
    _create_repo_with_diverging_branches(repo)
    _start_rebase_and_assert_conflict(repo)

    (repo / "f.txt").write_text("resolved\n")
    _run(repo, "add", "f.txt")

    env = build_non_interactive_git_env()
    env["TERM"] = "dumb"
    # Hermetically isolate this reproduction from ANY global/system git
    # config (e.g. a machine-wide `core.editor=true`) that could otherwise
    # mask the exact failure class this test exists to catch -- the test
    # must fail on the pre-fix code REGARDLESS of what's configured on the
    # host running the suite.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    result = subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=REBASE_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, (
        "git rebase --continue failed using build_non_interactive_git_env()'s "
        f"output: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Terminal is dumb" not in result.stderr
