"""Unit tests for Bug #696: git_diff handler silently drops revision parameters.

Verifies that the git_diff handler in git_read.py correctly forwards all
schema-advertised parameters to git_operations_service.git_diff():
  - from_revision, to_revision
  - stat_only
  - path (path filter)
  - context_lines

Also verifies:
  - Missing from_revision returns a validation error.
  - Invalid revision triggers GitCommandError branch and returns error dict.

Uses real git repos in tmp_path — NO MagicMock for git operations.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, cast
from unittest.mock import patch

from code_indexer.server.auth.user_manager import User, UserRole

# Module paths for patching
_LEGACY_MOD = "code_indexer.server.mcp.handlers._legacy"
_GIT_READ_SVC = "code_indexer.server.mcp.handlers.git_read.git_operations_service"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _extract(mcp_response: dict) -> dict:
    """Unwrap MCP content envelope to the actual response dict."""
    if "content" in mcp_response and mcp_response["content"]:
        text = mcp_response["content"][0].get("text", "")
        try:
            return cast(dict, json.loads(text))
        except json.JSONDecodeError:
            return {"text": text}
    return mcp_response


def _git(args: list, cwd: Path, env_extra: Optional[dict] = None) -> None:
    """Run a git command in cwd, raising on failure."""
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env=env,
    )


def _make_real_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """Create a real git repo with 3 commits.

    Commit layout:
      commit 0: add file_a.txt and file_b.txt
      commit 1: modify file_a.txt only
      commit 2: modify file_b.txt only

    Returns (repo_path, [sha0, sha1, sha2]).
    """
    repo = tmp_path / "test_repo"
    repo.mkdir()

    _git(["init", "--initial-branch=main"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)

    # Commit 0: add both files
    (repo / "file_a.txt").write_text("line1\nline2\nline3\n")
    (repo / "file_b.txt").write_text("alpha\nbeta\ngamma\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "initial: add file_a and file_b"], cwd=repo)

    # Commit 1: modify file_a only
    (repo / "file_a.txt").write_text("line1\nline2_modified\nline3\n")
    _git(["add", "file_a.txt"], cwd=repo)
    _git(["commit", "-m", "update file_a"], cwd=repo)

    # Commit 2: modify file_b only
    (repo / "file_b.txt").write_text("alpha\nbeta_modified\ngamma\n")
    _git(["add", "file_b.txt"], cwd=repo)
    _git(["commit", "-m", "update file_b"], cwd=repo)

    # Collect SHAs
    result = subprocess.run(
        ["git", "log", "--format=%H", "--reverse"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    shas = result.stdout.strip().splitlines()
    assert len(shas) == 3, f"Expected 3 commits, got {len(shas)}"
    return repo, shas


# ---------------------------------------------------------------------------
# Fixture: patch _resolve_git_repo_path to return the real repo path.
# The handler reaches _resolve_git_repo_path via _get_legacy()._resolve_git_repo_path,
# which ultimately calls code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path.
# ---------------------------------------------------------------------------


def _patch_resolve(repo_path: Path):
    """Return a context manager that makes _resolve_git_repo_path point at repo_path."""
    return patch(
        f"{_LEGACY_MOD}._resolve_git_repo_path",
        return_value=(str(repo_path), None),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitDiffHandlerForwarding:
    """Verify that git_diff handler forwards all schema parameters to the service."""

    def test_git_diff_forwards_from_revision_and_to_revision(self, tmp_path: Path):
        """Handler must forward from_revision/to_revision; diff between commits must be non-empty."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, shas = _make_real_repo(tmp_path)
        user = _make_user()

        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": shas[0],
                        "to_revision": shas[1],
                    },
                    user,
                )
            )

        assert result["success"] is True, result.get("error")
        assert result["files_changed"] > 0
        assert "diff_text" in result
        assert len(result["diff_text"]) > 0

    def test_git_diff_forwards_stat_only(self, tmp_path: Path):
        """Handler must forward stat_only=True; response must be a stat summary."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, shas = _make_real_repo(tmp_path)
        user = _make_user()

        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": shas[0],
                        "to_revision": shas[2],
                        "stat_only": True,
                    },
                    user,
                )
            )

        assert result["success"] is True, result.get("error")
        diff_text = result["diff_text"]
        # git --stat output contains "changed" for file count summary
        assert "changed" in diff_text or "|" in diff_text, (
            f"Expected stat output, got: {diff_text!r}"
        )

    def test_git_diff_stat_only_reports_nonzero_files_changed_count(
        self, tmp_path: Path
    ):
        """Regression for files_changed counter: when stat_only=True, the
        response metadata files_changed must reflect the actual file count
        reported by git --stat, not zero.

        Background: git_operations_service.git_diff previously computed
        files_changed by counting 'diff --git' markers in the diff output.
        That pattern only appears in unified-diff format, so --stat output
        (which has no 'diff --git' markers) always reported files_changed=0
        even though the stat summary line clearly shows 'N files changed'.
        """
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, shas = _make_real_repo(tmp_path)
        # shas[0]..shas[2] spans 2 modifications to 2 different files
        # (file_a.txt at commit 1 and file_b.txt at commit 2).
        user = _make_user()

        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": shas[0],
                        "to_revision": shas[2],
                        "stat_only": True,
                    },
                    user,
                )
            )

        assert result["success"] is True, result.get("error")
        # The diff spans file_a.txt and file_b.txt (2 files changed).
        # Before fix: files_changed=0 (counter looks for 'diff --git' markers
        # which don't appear in --stat output). After fix: files_changed=2.
        assert result["files_changed"] == 2, (
            f"Expected files_changed=2 for stat_only diff, got "
            f"{result['files_changed']}. diff_text={result['diff_text']!r}"
        )

    def test_git_diff_forwards_path_filter(self, tmp_path: Path):
        """Handler must forward path filter; only the filtered file should appear in diff."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, shas = _make_real_repo(tmp_path)
        user = _make_user()

        # Diff covers shas[1] (file_a change) and shas[2] (file_b change)
        # With path=file_a.txt, only file_a should appear.
        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": shas[0],
                        "to_revision": shas[2],
                        "path": "file_a.txt",
                    },
                    user,
                )
            )

        assert result["success"] is True, result.get("error")
        diff_text = result["diff_text"]
        assert "file_a.txt" in diff_text
        assert "file_b.txt" not in diff_text

    def test_git_diff_forwards_context_lines(self, tmp_path: Path):
        """Handler must forward context_lines; larger context produces more output lines."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, shas = _make_real_repo(tmp_path)
        user = _make_user()

        with _patch_resolve(repo):
            result_0 = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": shas[0],
                        "to_revision": shas[1],
                        "context_lines": 0,
                    },
                    user,
                )
            )
            result_5 = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": shas[0],
                        "to_revision": shas[1],
                        "context_lines": 5,
                    },
                    user,
                )
            )

        assert result_0["success"] is True
        assert result_5["success"] is True
        lines_0 = result_0["diff_text"].count("\n")
        lines_5 = result_5["diff_text"].count("\n")
        assert lines_5 > lines_0, (
            f"Expected more output with context_lines=5 ({lines_5}) than "
            f"context_lines=0 ({lines_0})"
        )

    def test_git_diff_invalid_revision_returns_error(self, tmp_path: Path):
        """An invalid revision must trigger GitCommandError and return success=False."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, _ = _make_real_repo(tmp_path)
        user = _make_user()

        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": "nonexistent-sha-xyz",
                    },
                    user,
                )
            )

        assert result["success"] is False
        # Either error_type or error key must indicate the failure
        assert "error" in result

    def test_git_diff_missing_from_revision_returns_error(self, tmp_path: Path):
        """Missing from_revision must return validation error without calling git."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, _ = _make_real_repo(tmp_path)
        user = _make_user()

        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        # from_revision deliberately omitted
                    },
                    user,
                )
            )

        assert result["success"] is False
        assert "from_revision" in result["error"]
        assert "Missing required parameter" in result["error"]

    def test_git_diff_with_from_revision_head_works(self, tmp_path: Path):
        """Calling with from_revision='HEAD' should succeed (working-tree vs HEAD)."""
        from code_indexer.server.mcp.handlers.git_read import git_diff

        repo, _ = _make_real_repo(tmp_path)
        user = _make_user()

        with _patch_resolve(repo):
            result = _extract(
                git_diff(
                    {
                        "repository_alias": "test-repo",
                        "from_revision": "HEAD",
                    },
                    user,
                )
            )

        # Clean working tree: diff vs HEAD is empty but succeeds
        assert result["success"] is True
