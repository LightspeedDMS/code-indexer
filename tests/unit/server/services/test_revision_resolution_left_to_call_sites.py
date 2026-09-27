"""`validate_revision` (git_argv_safety.py) checks only two hazards -- a
leading '-' (unless the shorthand flag is set) and a NUL/CR/LF/other C0
control character -- and never resolves the value itself. Resolving or
rejecting an unresolvable revision is left entirely to each call site's
own pre-existing logic (or to `git` itself), exactly as it was before this
validator existed. This file pins that outcome at the five call sites
where it is externally observable, through the REAL front door (REST /
MCP dispatch or the real service method) against REAL throwaway git
repositories (no mocking of git itself, per this repo's Anti-Mock rule):

  1. REST GET .../git/cat with an unresolvable rev -> 404 (this route's
     own subsequent `git rev-parse`/`git show` calls fail, not the
     validator).
  2. REST GET .../git/cat with rev set to a two-dot range -> 200 with
     empty content (a `git show <range>:<path>` quirk: both `git
     rev-parse` and `git show` succeed on a range, but `git show` prints
     nothing for it).
  3. `GitOperationsService.git_reset` with an unresolvable commit_hash ->
     `GitCommandError` from the `git reset` subprocess itself, never a
     `ValueError` from the validator.
  4. MCP `git_merge` with an unresolvable source_branch -> the structured
     `error_type`/`stderr`/`command` shape `_handle_write_error` builds
     from the real `GitCommandError` `git merge` itself raises.
  5. MCP `git_show_commit` / `git_file_at_revision` with an unresolvable
     commit_hash/revision -> the original `ValueError` message text each
     handler's own internal resolution logic has always raised
     ("Commit not found: ..." / "Invalid revision: ...").
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    git_operations_service,
)

_SESSION_ID = "mcp-session-revision-resolution-left-to-call-sites"
_BASE = "/api/v1/repos/myrepo/git"


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    _git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c2"], repo)
    _git(["checkout", "-q", "main"], repo)
    return repo


# ---------------------------------------------------------------------------
# Sites 1-2: REST GET .../git/cat
# ---------------------------------------------------------------------------


@contextmanager
def _arm_repo_rest(repo_path: Path):
    with patch(
        "code_indexer.server.routers.git._get_activated_repo_manager"
    ) as mock_getter:
        mock_arm = Mock()
        mock_arm.get_activated_repo_path.return_value = str(repo_path)
        mock_getter.return_value = mock_arm
        yield mock_arm


@pytest.fixture()
def mock_user():
    user = Mock()
    user.username = "testuser"
    return user


@pytest.fixture()
def test_client(mock_user):
    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestGitCatUnresolvableRevision:
    def test_nonexistent_revision_returns_404_with_no_git_argv_in_body(
        self, test_client, tmp_path
    ):
        repo = _make_repo(tmp_path)
        with _arm_repo_rest(repo):
            response = test_client.get(
                f"{_BASE}/cat",
                params={"path": "f.txt", "rev": "not-a-real-revision-xyz"},
            )
        assert response.status_code == 404, response.text
        detail = response.json()["detail"]
        assert detail == "File or revision not found: f.txt@not-a-real-revision-xyz"
        # The detail is this route's own pre-existing 404 message, not a
        # validator message: it must never echo a git command line.
        assert "git" not in detail.lower()


class TestGitCatRangeShapedRevision:
    def test_two_dot_range_returns_200_with_empty_content(self, test_client, tmp_path):
        """`git rev-parse main..feature` succeeds (prints the two boundary
        SHAs, one per line, exit 0), and `git show main..feature:f.txt`
        also succeeds but prints nothing -- a real `git show` quirk, not
        an error. `validate_revision` never resolves the value itself, so
        this range-shaped rev reaches those two subprocess calls
        unchanged and gets this same 200-with-empty-content outcome, not
        a validation rejection."""
        repo = _make_repo(tmp_path)
        with _arm_repo_rest(repo):
            response = test_client.get(
                f"{_BASE}/cat",
                params={"path": "f.txt", "rev": "main..feature"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["content"] == ""


# ---------------------------------------------------------------------------
# Site 3: service-level git_reset
# ---------------------------------------------------------------------------


class TestGitResetUnresolvableCommit:
    def test_unresolvable_commit_hash_raises_git_command_error(self, tmp_path):
        """`git_reset`'s `validate_revision` call checks only a leading
        '-' and control characters -- it never resolves commit_hash
        itself, so an unresolvable value reaches the `git reset`
        subprocess unchanged and surfaces as that subprocess's own
        failure (`GitCommandError`), exactly as it did with no
        validation at all -- never a `ValueError` from the validator."""
        repo = _make_repo(tmp_path)
        with pytest.raises(GitCommandError):
            git_operations_service.git_reset(
                repo, mode="mixed", commit_hash="not-a-real-revision-xyz"
            )


# ---------------------------------------------------------------------------
# Sites 4-5: MCP git_merge / git_show_commit / git_file_at_revision
# ---------------------------------------------------------------------------


def _mock_user() -> MagicMock:
    user = MagicMock()
    user.username = "user_a"
    user.has_permission.return_value = True
    return user


def _no_impersonation_session_state() -> MagicMock:
    session_state = MagicMock()
    session_state.is_impersonating = False
    return session_state


def _parse_mcp_response(response: Dict[str, Any]) -> Dict[str, Any]:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


@asynccontextmanager
async def _real_dispatch_context(
    tool_name: str, handler_fn: Any, required_permission: str
):
    from code_indexer.server.mcp.protocol import handle_tools_call

    with (
        patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {tool_name: handler_fn},
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                tool_name: {
                    "required_permission": required_permission,
                    "name": tool_name,
                }
            },
            create=True,
        ),
        patch("code_indexer.server.mcp.protocol._check_repository_access"),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch("code_indexer.server.mcp.protocol.api_metrics_service"),
        patch(
            "code_indexer.server.app.app.state.access_filtering_service",
            MagicMock(),
            create=True,
        ),
    ):
        yield _mock_user(), handle_tools_call


class TestGitMergeUnresolvableSource:
    @pytest.mark.asyncio
    async def test_unresolvable_source_branch_returns_structured_git_command_error(
        self, tmp_path: Path
    ):
        import code_indexer.server.mcp.handlers.git_write as git_write

        repo = _make_repo(tmp_path)

        async with _real_dispatch_context(
            "git_merge", git_write.git_merge, "repository:write"
        ) as (user, handle_tools_call):
            with (
                patch(
                    "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
                    return_value=(str(repo), None),
                ),
                patch(
                    "code_indexer.server.mcp.handlers.git_write._check_writable",
                    return_value=None,
                ),
            ):
                result = await handle_tools_call(
                    params={
                        "name": "git_merge",
                        "arguments": {
                            "repository_alias": "example-repo-global",
                            "source_branch": "not-a-real-branch-xyz",
                        },
                    },
                    user=user,
                    session_id=_SESSION_ID,
                    session_state=_no_impersonation_session_state(),
                )

        parsed = _parse_mcp_response(result)
        # `merge_branch`'s `validate_revision` call never resolves
        # source_branch itself, so this reaches the real `git merge`
        # subprocess unchanged and fails there, surfacing as the same
        # structured GitCommandError shape it always did.
        assert parsed.get("success") is False
        assert parsed.get("error_type") == "GitCommandError"
        assert "stderr" in parsed
        assert "command" in parsed


async def _call_show_commit(repo: Path, commit_hash: str) -> Dict[str, Any]:
    import code_indexer.server.mcp.handlers.git_read as git_read

    async with _real_dispatch_context(
        "git_show_commit", git_read.handle_git_show_commit, "query_repos"
    ) as (user, handle_tools_call):
        with patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=(str(repo), None),
        ):
            result = await handle_tools_call(
                params={
                    "name": "git_show_commit",
                    "arguments": {
                        "repository_alias": "example-repo-global",
                        "commit_hash": commit_hash,
                    },
                },
                user=user,
                session_id=_SESSION_ID,
                session_state=_no_impersonation_session_state(),
            )
    return _parse_mcp_response(result)


async def _call_file_at_revision(
    repo: Path, path: str, revision: str
) -> Dict[str, Any]:
    import code_indexer.server.mcp.handlers.git_read as git_read

    async with _real_dispatch_context(
        "git_file_at_revision",
        git_read.handle_git_file_at_revision,
        "query_repos",
    ) as (user, handle_tools_call):
        with patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=(str(repo), None),
        ):
            result = await handle_tools_call(
                params={
                    "name": "git_file_at_revision",
                    "arguments": {
                        "repository_alias": "example-repo-global",
                        "path": path,
                        "revision": revision,
                    },
                },
                user=user,
                session_id=_SESSION_ID,
                session_state=_no_impersonation_session_state(),
            )
    return _parse_mcp_response(result)


@pytest.mark.asyncio
class TestGitShowCommitUnresolvableCommit:
    async def test_returns_original_commit_not_found_message(self, tmp_path: Path):
        """`validate_revision` checks only a leading '-' and control
        characters -- it never resolves commit_hash itself, so this
        service method's OWN internal `git rev-parse` call is what
        raises this exact `ValueError` message, and the handler's
        generic `except ValueError` surfaces it unchanged."""
        repo = _make_repo(tmp_path)
        parsed = await _call_show_commit(repo, "not-a-real-revision-xyz")
        assert parsed.get("success") is not True
        assert parsed.get("error") == "Commit not found: not-a-real-revision-xyz"


@pytest.mark.asyncio
class TestGitFileAtRevisionUnresolvableRevision:
    async def test_returns_original_invalid_revision_message(self, tmp_path: Path):
        """Same reasoning as show_commit above: `get_file_at_revision`'s
        own pre-existing `git rev-parse --verify` call raises this exact
        `ValueError` message, unaffected by the validator simplification."""
        repo = _make_repo(tmp_path)
        parsed = await _call_file_at_revision(repo, "f.txt", "not-a-real-revision-xyz")
        assert parsed.get("success") is not True
        assert parsed.get("error") == "Invalid revision: not-a-real-revision-xyz"
