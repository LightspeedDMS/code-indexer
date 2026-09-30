"""MCP git read tools log a rejected option-shaped argument the way REST does.

A value rejected by git argv validation (GitArgumentValidationError) is a
client input error: the REST routes log it at WARNING with the validation
message and no stack trace. The MCP git read handlers must do the same --
WARNING, no traceback, no ERROR record -- and keep returning the same
structured error payload.

Driven through the real MCP tools/call dispatcher against a real throwaway
git repository. Only alias-to-path resolution and MCP plumbing not under
test (repository access check, tracing, API metrics) are patched.
"""

from __future__ import annotations

import json
import logging
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

_GIT_READ_LOGGER = "code_indexer.server.mcp.handlers.git_read"


def _git(args: List[str], cwd: Path) -> None:
    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "first"], repo)
    return repo


@asynccontextmanager
async def _dispatch(tool_name: str, handler_fn: Any, repo: Path) -> AsyncIterator[Any]:
    from code_indexer.server.mcp.protocol import handle_tools_call

    with (
        patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {tool_name: handler_fn},
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {tool_name: {"required_permission": "query_repos", "name": tool_name}},
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
        patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            return_value=(str(repo), None),
        ),
    ):
        yield handle_tools_call


def _user() -> MagicMock:
    user = MagicMock()
    user.username = "user_a"
    user.has_permission.return_value = True
    return user


def _session_state() -> MagicMock:
    state = MagicMock()
    state.is_impersonating = False
    return state


def _handler(name: str) -> Any:
    import code_indexer.server.mcp.handlers.git_read as git_read

    return getattr(git_read, name)


# (tool name, handler attribute, arguments carrying an option-shaped value)
_CASES: List[Tuple[str, str, Dict[str, Any]]] = [
    ("git_diff", "git_diff", {"from_revision": "--output=/tmp/x"}),
    ("git_log", "git_log", {"branch": "--output=/tmp/x"}),
    ("git_log", "handle_git_log", {"branch": "--output=/tmp/x"}),
    ("git_show_commit", "handle_git_show_commit", {"commit_hash": "--output=/tmp/x"}),
    (
        "git_file_at_revision",
        "handle_git_file_at_revision",
        {"path": "f.txt", "revision": "--output=/tmp/x"},
    ),
    (
        "git_blame",
        "handle_git_blame",
        {"path": "f.txt", "revision": "--output=/tmp/x"},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,handler_name,extra_args", _CASES)
async def test_rejection_logged_at_warning_without_traceback(
    tmp_path: Path, caplog: Any, tool: str, handler_name: str, extra_args: Dict
) -> None:
    repo = _make_repo(tmp_path)
    caplog.set_level(logging.DEBUG, logger=_GIT_READ_LOGGER)

    async with _dispatch(tool, _handler(handler_name), repo) as handle_tools_call:
        result = await handle_tools_call(
            params={
                "name": tool,
                "arguments": {"repository_alias": "example-repo", **extra_args},
            },
            user=_user(),
            session_id="git-read-rejection-logging",
            session_state=_session_state(),
        )

    payload = json.loads(result["content"][0]["text"])
    assert set(payload) == {"success", "error"}, payload
    assert payload["success"] is False
    assert "must not start with '-'" in payload["error"]

    records = [r for r in caplog.records if r.name == _GIT_READ_LOGGER]
    assert not [r for r in records if r.levelno >= logging.ERROR], [
        r.getMessage() for r in records
    ]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in records]
    assert warnings[0].exc_info is None
    assert "must not start with '-'" in warnings[0].getMessage()
