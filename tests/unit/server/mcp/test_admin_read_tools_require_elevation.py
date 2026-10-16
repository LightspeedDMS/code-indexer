"""MCP admin reads of group membership and audit history require elevation.

``get_group`` (member usernames), ``scip_pr_history`` and
``scip_cleanup_history`` (audit-log content) are the MCP twins of the
elevation-gated REST reads ``GET /api/v1/groups/{group_id}``,
``GET /api/admin/scip-pr-history`` and ``GET /api/admin/scip-git-cleanup-history``.
With enforcement on, an admin without their own elevation window gets
``elevation_required`` and no data; with the window the data comes back; a
non-admin is refused whatever window they hold; enforcement off serves the
data as before.

Front door: the real MCP JSON-RPC ``tools/call`` dispatcher over real auth
components (see self_service_elevation_harness), a real GroupAccessManager
and a real AuditLogService on per-test files.  Only the elevation-enforcement
switch is patched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.mcp.handlers import _utils as mcp_utils
from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.group_access_manager import GroupAccessManager
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
)

_ADMIN = "example-reads-admin"
_MEMBER = "example-reads-member"
_PR_URL = "https://example.com/example-org/example-repo/pull/7"
_CLEANED_PATH = "/example/workspaces/example-repo"


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SelfServiceStack:
    s = build_stack(tmp_path, monkeypatch)
    s.create_user(_ADMIN, UserRole.ADMIN)
    s.enroll_mfa(_ADMIN)
    s.create_user(_MEMBER)
    s.enroll_mfa(_MEMBER)
    return s


@pytest.fixture
def tools(
    stack: SelfServiceStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Dict[str, Dict[str, Any]]:
    """Real group and audit stores on this test's files; returns, per tool,
    its arguments and a value only its successful result carries."""
    groups = GroupAccessManager(tmp_path / "groups.db")
    group = groups.create_group("example-team", "Example team")
    groups.assign_user_to_group(_MEMBER, group.id, _ADMIN)

    audit = AuditLogService(tmp_path / "audit.db")
    audit.log(
        "system",
        "pr_creation_success",
        "repo",
        "example-repo-global",
        details=json.dumps({"pr_url": _PR_URL, "repo_alias": "example-repo-global"}),
    )
    audit.log(
        "system",
        "git_cleanup",
        "repo",
        _CLEANED_PATH,
        details=json.dumps({"repo_path": _CLEANED_PATH, "files_cleared": ["a"]}),
    )

    state = mcp_utils.app_module.app.state
    monkeypatch.setattr(state, "group_manager", groups, raising=False)
    monkeypatch.setattr(state, "audit_service", audit, raising=False)
    return {
        "get_group": {"args": {"group_id": str(group.id)}, "secret": _MEMBER},
        "scip_pr_history": {"args": {}, "secret": _PR_URL},
        "scip_cleanup_history": {"args": {}, "secret": _CLEANED_PATH},
    }


_TOOLS = ["get_group", "scip_pr_history", "scip_cleanup_history"]


def _call(tool: str, args: Dict[str, Any], username: str, stack, key: Optional[str]):
    """The full JSON-RPC response of a real tools/call, as text and payload."""
    from code_indexer.server.mcp.protocol import process_jsonrpc_request

    user = stack.user_manager.get_user(username)
    response = asyncio.run(
        process_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            user,
            elevation_key=key,
        )
    )
    text = json.dumps(response)
    payload: Dict[str, Any] = {}
    if "result" in response:
        payload = json.loads(response["result"]["content"][0]["text"])
    return text, payload


def _window(stack: SelfServiceStack, username: str) -> str:
    key = f"example-key-{username}"
    stack.elevate(key, username)
    return key


@pytest.mark.parametrize("tool", _TOOLS)
def test_admin_without_window_gets_elevation_required(stack, tools, tool) -> None:
    with enforcement(True):
        text, payload = _call(tool, tools[tool]["args"], _ADMIN, stack, "no-window")
    assert payload.get("error") == "elevation_required", text[:400]
    assert tools[tool]["secret"] not in text


@pytest.mark.parametrize("tool", _TOOLS)
def test_admin_with_own_window_gets_data(stack, tools, tool) -> None:
    key = _window(stack, _ADMIN)
    with enforcement(True):
        text, payload = _call(tool, tools[tool]["args"], _ADMIN, stack, key)
    assert payload.get("success") is True, text[:400]
    assert tools[tool]["secret"] in text


@pytest.mark.parametrize("tool", _TOOLS)
def test_normal_user_with_own_window_is_refused(stack, tools, tool) -> None:
    key = _window(stack, _MEMBER)
    with enforcement(True):
        text, payload = _call(tool, tools[tool]["args"], _MEMBER, stack, key)
    assert payload.get("success") is not True, text[:400]
    assert payload.get("error") != "elevation_required", text[:400]
    if tool != "get_group":  # the member's own name appears in no refusal
        assert tools[tool]["secret"] not in text
    assert '"members"' not in text


@pytest.mark.parametrize("tool", _TOOLS)
def test_enforcement_off_serves_data_without_window(stack, tools, tool) -> None:
    with enforcement(False):
        text, payload = _call(tool, tools[tool]["args"], _ADMIN, stack, None)
    assert payload.get("success") is True, text[:400]
    assert tools[tool]["secret"] in text
