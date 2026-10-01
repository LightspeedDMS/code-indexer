"""MCP `git_blame` (global_repos `GitOperationsService.get_blame`) must
accept a two-dot/three-dot revision RANGE in `revision`, and the `^!`
single-revision suffix, since `git blame` itself accepts both (restricting
the blame to that range, with boundary commits marked `^`): each
non-empty side must resolve and never start with '-'.

Driven through the REAL MCP protocol dispatcher (`protocol.handle_tools_call`
-> the registered `handle_git_blame` handler) against a REAL throwaway git
repository (no mocking of git itself, per this repo's Anti-Mock rule).
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

_SESSION_ID = "mcp-session-git-blame-revision-range"


def _git(args: list, cwd: Path) -> None:
    subprocess.run(
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
    _git(["commit", "-q", "-m", "c1 on main"], repo)
    _git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c2 on feature"], repo)
    _git(["checkout", "-q", "main"], repo)
    return repo


def _parse_mcp_response(response: Dict[str, Any]) -> Dict[str, Any]:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _mock_user() -> MagicMock:
    user = MagicMock()
    user.username = "user_a"
    user.has_permission.return_value = True
    return user


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


def _no_impersonation_session_state() -> MagicMock:
    session_state = MagicMock()
    session_state.is_impersonating = False
    return session_state


async def _call_git_blame(repo: Path, revision: str) -> Dict[str, Any]:
    import code_indexer.server.mcp.handlers.git_read as git_read

    async with _real_dispatch_context(
        "git_blame", git_read.handle_git_blame, "query_repos"
    ) as (user, handle_tools_call):
        with patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=(str(repo), None),
        ):
            result = await handle_tools_call(
                params={
                    "name": "git_blame",
                    "arguments": {
                        "repository_alias": "example-repo-global",
                        "path": "f.txt",
                        "revision": revision,
                    },
                },
                user=user,
                session_id=_SESSION_ID,
                session_state=_no_impersonation_session_state(),
            )
    return _parse_mcp_response(result)


@pytest.mark.asyncio
class TestGitBlameRevisionRangeFrontDoor:
    async def test_two_dot_range_accepted(self, tmp_path: Path):
        repo = _make_repo(tmp_path)
        parsed = await _call_git_blame(repo, "main..feature")
        assert parsed.get("success") is True, parsed
        assert len(parsed.get("lines", [])) == 2

    async def test_caret_bang_suffix_accepted(self, tmp_path: Path):
        repo = _make_repo(tmp_path)
        parsed = await _call_git_blame(repo, "feature^!")
        assert parsed.get("success") is True, parsed
        assert len(parsed.get("lines", [])) == 2

    async def test_accepts_range_with_option_shaped_side_deferred_to_git(
        self, tmp_path: Path
    ):
        """`main..-x` is not an option: the whole value starts with 'm'.
        There is no per-side check, so this reaches `git blame` unchanged
        and fails there instead, surfacing as `success: False` -- the
        same as before any validation existed."""
        repo = _make_repo(tmp_path)
        parsed = await _call_git_blame(repo, "main..-x")
        assert parsed.get("success") is not True

    async def test_rejects_whole_value_starting_with_dash(self, tmp_path: Path):
        repo = _make_repo(tmp_path)
        parsed = await _call_git_blame(repo, "-x..feature")
        assert parsed.get("success") is not True
        assert "must not start with '-'" in str(parsed.get("error", ""))
