"""Unit tests for Story #926 Claude conflict resolver."""

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


def test_resolve_defensive_check_unmerged_paths(tmp_path):
    """# Story #926 AC5: resolver fails if git still reports unmerged files after Claude returns success."""
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()

    with (
        patch(
            "code_indexer.server.services.cidx_meta_backup.conflict_resolver.invoke_claude_cli",
            return_value=(True, "done"),
        ),
        patch(
            "code_indexer.server.services.cidx_meta_backup.conflict_resolver.subprocess.run",
            return_value=CompletedProcess(
                args=["git"], returncode=0, stdout="docs/a.md\n", stderr=""
            ),
        ),
    ):
        result = ClaudeConflictResolver().resolve(str(repo), ["docs/a.md"], "master")

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
