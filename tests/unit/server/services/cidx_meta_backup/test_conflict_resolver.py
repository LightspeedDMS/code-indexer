"""Unit tests for Story #926 Claude conflict resolver."""

import subprocess as _subprocess
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


def test_resolve_success_calls_invoke_claude_cli(tmp_path):
    """# Story #926 AC4: resolver delegates conflict resolution to invoke_claude_cli and succeeds when conflicts are cleared."""
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()

    with (
        patch(
            "code_indexer.server.services.cidx_meta_backup.conflict_resolver.invoke_claude_cli",
            return_value=(True, "resolved"),
        ) as invoke_mock,
        patch(
            "code_indexer.server.services.cidx_meta_backup.conflict_resolver.subprocess.run",
            return_value=CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr=""
            ),
        ),
    ):
        result = ClaudeConflictResolver().resolve(
            str(repo), ["docs/a.md", "docs/b.md"], "master"
        )

    assert result.success is True
    assert result.error is None
    invoke_mock.assert_called_once()


def test_resolve_timeout_returns_failure(tmp_path):
    """# Story #926 AC5: resolver surfaces a Claude CLI timeout as a failure result."""
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()

    with (
        patch(
            "code_indexer.server.services.cidx_meta_backup.conflict_resolver.invoke_claude_cli",
            return_value=(False, "Claude resolver timed out after 600s"),
        ),
        patch(
            "code_indexer.server.services.cidx_meta_backup.conflict_resolver.subprocess.run",
            return_value=CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr=""
            ),
        ),
    ):
        result = ClaudeConflictResolver().resolve(str(repo), ["docs/a.md"], "master")

    assert result.success is False
    assert "timed out" in str(result.error)


# git merge exits with this code when it leaves the index in conflict state (UU).
_GIT_MERGE_CONFLICT_EXIT_CODE = 1


def _git_raw(args: list, cwd, check: bool = True):
    return _subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def _init_conflict_repo(repo, conflict_file: str) -> None:
    """Set up a real git repo with two branches that conflict on conflict_file."""
    repo.mkdir(exist_ok=True)
    _git_raw(["init", "-b", "master"], repo)
    _git_raw(["config", "user.email", "test@test.invalid"], repo)
    _git_raw(["config", "user.name", "Test"], repo)

    (repo / conflict_file).parent.mkdir(parents=True, exist_ok=True)
    (repo / conflict_file).write_text("original\n")
    _git_raw(["add", "-A"], repo)
    _git_raw(["commit", "-m", "base"], repo)

    _git_raw(["checkout", "-b", "feature"], repo)
    (repo / conflict_file).write_text("feature change\n")
    _git_raw(["add", "-A"], repo)
    _git_raw(["commit", "-m", "feature"], repo)

    _git_raw(["checkout", "master"], repo)
    (repo / conflict_file).write_text("master change\n")
    _git_raw(["add", "-A"], repo)
    _git_raw(["commit", "-m", "master"], repo)

    merge_result = _subprocess.run(
        ["git", "merge", "--no-commit", "feature"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge_result.returncode == _GIT_MERGE_CONFLICT_EXIT_CODE, (
        f"Expected conflicting merge (exit {_GIT_MERGE_CONFLICT_EXIT_CODE}), "
        f"got {merge_result.returncode}: {merge_result.stderr}"
    )


def _assert_uu_conflict_state(repo, conflict_file: str) -> None:
    """Assert the repo index has a genuine UU (unmerged) entry for conflict_file."""
    result = _subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert conflict_file in result.stdout, (
        f"Test setup failed: expected UU state for {conflict_file!r}, "
        f"git diff output: {result.stdout!r}"
    )


def test_resolve_defensive_check_unmerged_paths(tmp_path):
    """# Story #926 AC5: resolver fails if git still reports unmerged files after Claude returns success.

    Uses a real git repo in genuine UU conflict state (via git merge --no-commit).
    Only invoke_claude_cli is patched; subprocess.run is NOT patched so the
    defensive git diff check runs against real git output.
    """
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    conflict_file = "docs/a.md"
    repo = tmp_path / "cidx-meta"
    _init_conflict_repo(repo, conflict_file)
    _assert_uu_conflict_state(repo, conflict_file)

    with patch(
        "code_indexer.server.services.cidx_meta_backup.conflict_resolver.invoke_claude_cli",
        return_value=(True, "done"),
    ):
        result = ClaudeConflictResolver().resolve(str(repo), [conflict_file], "master")

    assert result.success is False
    assert result.error == "Claude did not resolve all conflicts"


def test_resolve_uses_externalized_prompt():
    """# Story #926 AC4: conflict-resolution prompt is externalized in cidx_meta_conflict_resolution.md."""
    prompt_path = (
        Path(__file__).resolve().parents[5]
        / "src"
        / "code_indexer"
        / "server"
        / "mcp"
        / "prompts"
        / "cidx_meta_conflict_resolution.md"
    )

    assert prompt_path.exists(), "Missing external prompt file"
    content = prompt_path.read_text()
    assert "{conflict_files}" in content
    assert "{branch}" in content
    assert "{repo_path}" in content
