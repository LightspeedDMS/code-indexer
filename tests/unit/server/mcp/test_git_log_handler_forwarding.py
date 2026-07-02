"""Unit tests for Bug #697: git_log handler silently drops filter parameters
and uses wrong key name for since parameter.

Two defects fixed:
  Defect 1: handler did not forward path, author, since, until, branch.
  Defect 2: handler read args.get("since_date") but schema param is "since".
             Fix reads args.get("since") and passes it as since_date= to service.

Uses real git repos in tmp_path — NO MagicMock for git operations.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

from code_indexer.server.auth.user_manager import User, UserRole

_LEGACY_MOD = "code_indexer.server.mcp.handlers._legacy"


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
    """Unwrap MCP content envelope. cast<dict> is safe: json.loads returns dict for object JSON."""
    if "content" in mcp_response and mcp_response["content"]:
        text = mcp_response["content"][0].get("text", "")
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"text": text}
        except json.JSONDecodeError:
            return {"text": text}
    return mcp_response


def _git(args: List[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True)


def _patch_resolve(repo_path: Path):
    return patch(
        f"{_LEGACY_MOD}._resolve_git_repo_path",
        return_value=(str(repo_path), None),
    )


def _call_git_log(repo: Path, args: dict) -> dict:
    """Call git_log handler with patched repo path, return unwrapped result."""
    from code_indexer.server.mcp.handlers.git_read import git_log

    with _patch_resolve(repo):
        return _extract(
            git_log({"repository_alias": "test-repo", **args}, _make_user())
        )


def _init_repo(repo: Path) -> None:
    _git(["init", "--initial-branch=main"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)


def _commit(
    repo: Path, name: str, email: str, file: str, content: str, msg: str
) -> str:
    """Write file, stage, commit as name/email, return the new commit SHA."""
    (repo / file).write_text(content)
    _git(["add", file], cwd=repo)
    _git(
        ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", msg],
        cwd=repo,
    )
    r = subprocess.run(
        ["git", "log", "--format=%H", "-1"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def _make_repo(tmp_path: Path) -> Tuple[Path, List[str], str]:
    """Create repo with 3 commits on main + 1-commit feature branch.

    main:    sha0 (Alice/file_a.txt), sha1 (Bob/file_b.txt), sha2 (Alice/file_a.txt modify)
    feature: branched from sha0, sha_f (Charlie/feat.txt)

    Returns (repo_path, [sha0, sha1, sha2], feature_sha).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sha0 = _commit(
        repo, "Alice", "alice@x.com", "file_a.txt", "v1\n", "Alice: initial file_a"
    )
    sha1 = _commit(repo, "Bob", "bob@x.com", "file_b.txt", "v1\n", "Bob: add file_b")
    sha2 = _commit(
        repo, "Alice", "alice@x.com", "file_a.txt", "v2\n", "Alice: modify file_a"
    )
    _git(["checkout", "-b", "feature", sha0], cwd=repo)
    sha_f = _commit(
        repo, "Charlie", "charlie@x.com", "feat.txt", "feat\n", "Charlie: feature"
    )
    _git(["checkout", "main"], cwd=repo)
    return repo, [sha0, sha1, sha2], sha_f


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitLogHandlerForwarding:
    """Verify git_log handler forwards all schema-advertised filter parameters."""

    def test_git_log_forwards_path_filter(self, tmp_path: Path) -> None:
        """path filter: only commits touching file_a.txt (2 of 3) should be returned."""
        repo, _, _ = _make_repo(tmp_path)
        result = _call_git_log(repo, {"path": "file_a.txt", "limit": 50})
        assert result["success"] is True, result.get("error")
        assert result["commits_returned"] == 2
        for c in result["commits"]:
            assert "Alice" in c["message"], (
                f"Non-Alice commit in file_a results: {c['message']}"
            )

    def test_git_log_forwards_author_filter(self, tmp_path: Path) -> None:
        """author filter: only Alice's 2 commits should be returned."""
        repo, _, _ = _make_repo(tmp_path)
        result = _call_git_log(repo, {"author": "Alice", "limit": 50})
        assert result["success"] is True, result.get("error")
        assert result["commits_returned"] == 2
        for c in result["commits"]:
            assert "Alice" in c["message"], (
                f"Non-Alice commit in author=Alice results: {c['message']}"
            )

    def test_git_log_forwards_since_param(self, tmp_path: Path) -> None:
        """since=far-future yields 0 commits; since=far-past yields all 3 commits."""
        repo, _, _ = _make_repo(tmp_path)

        r_future = _call_git_log(repo, {"since": "2099-01-01", "limit": 50})
        assert r_future["success"] is True, r_future.get("error")
        assert r_future["commits_returned"] == 0, (
            f"Expected 0 commits with future since, got {r_future['commits_returned']}"
        )

        r_past = _call_git_log(repo, {"since": "1970-01-01", "limit": 50})
        assert r_past["success"] is True, r_past.get("error")
        assert r_past["commits_returned"] == 3, (
            f"Expected 3 commits with past since, got {r_past['commits_returned']}"
        )

    def test_git_log_forwards_until_param(self, tmp_path: Path) -> None:
        """until=far-past yields 0 commits; until=far-future yields all 3 commits."""
        repo, _, _ = _make_repo(tmp_path)

        r_old = _call_git_log(repo, {"until": "1970-01-01", "limit": 50})
        assert r_old["success"] is True, r_old.get("error")
        assert r_old["commits_returned"] == 0, (
            f"Expected 0 commits with old until, got {r_old['commits_returned']}"
        )

        r_future = _call_git_log(repo, {"until": "2099-01-01", "limit": 50})
        assert r_future["success"] is True, r_future.get("error")
        assert r_future["commits_returned"] == 3, (
            f"Expected 3 commits with future until, got {r_future['commits_returned']}"
        )

    def test_git_log_forwards_branch_param(self, tmp_path: Path) -> None:
        """branch filter: feature branch has 2 commits (sha0 + Charlie's commit)."""
        repo, _, sha_f = _make_repo(tmp_path)
        result = _call_git_log(repo, {"branch": "feature", "limit": 50})
        assert result["success"] is True, result.get("error")
        commits = result["commits"]
        assert len(commits) == 2
        hashes = [c["commit_hash"] for c in commits]
        assert sha_f in hashes, f"Feature SHA {sha_f} not in {hashes}"
        messages = [c["message"] for c in commits]
        assert not any("Bob" in m for m in messages), (
            "Bob's main-only commit leaked into feature branch"
        )

    def test_git_log_invalid_branch_returns_error_or_empty(
        self, tmp_path: Path
    ) -> None:
        """Invalid branch must return success=False or commits_returned=0 — no crash."""
        repo, _, _ = _make_repo(tmp_path)
        result = _call_git_log(repo, {"branch": "nonexistent-zzz", "limit": 50})
        if result.get("success") is True:
            assert result.get("commits_returned", 0) == 0
        else:
            assert "error" in result

    def test_git_log_reads_since_not_since_date(self, tmp_path: Path) -> None:
        """KEY-MISMATCH REGRESSION GUARD.

        'since_date' (old broken key) must be IGNORED — filter not applied,
        all 3 commits returned.

        'since' (correct schema key) must be RESPECTED — future date, 0 commits.
        """
        repo, _, _ = _make_repo(tmp_path)

        # Old broken key — must be ignored (not forwarded), returns all 3
        r_old_key = _call_git_log(repo, {"since_date": "2099-01-01", "limit": 50})
        assert r_old_key["success"] is True, r_old_key.get("error")
        assert r_old_key["commits_returned"] == 3, (
            f"'since_date' should not be forwarded as a filter; "
            f"got {r_old_key['commits_returned']} instead of 3"
        )

        # Correct schema key — must be forwarded, returns 0
        r_new_key = _call_git_log(repo, {"since": "2099-01-01", "limit": 50})
        assert r_new_key["success"] is True, r_new_key.get("error")
        assert r_new_key["commits_returned"] == 0, (
            f"'since' should filter to 0 commits with future date; "
            f"got {r_new_key['commits_returned']}"
        )
