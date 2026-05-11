"""Phase 4 E2E: Epic #985 MCP consolidated endpoint regression against live server.

Exercises every endpoint modified in the MCP consolidation epic via real
MCP JSON-RPC calls to the live uvicorn server. Unlike Phase 3 (TestClient),
the live server has full service initialization (access filtering, group manager,
golden repos). Tests validate both happy paths and error paths.

Fixture dependencies:
  e2e_http_client + e2e_admin_token -- for raw MCP calls
  registered_golden_repo            -- ensures markupsafe is registered for repo_status tests
  activated_golden_repo             -- ensures markupsafe is activated

Story coverage:
  S987: cidx_quick_reference (tool param), first_time_user_guide (repository_status ref)
  S989: list_mcp_credentials (scope), manage_mcp_credential (action)
  S990: repository_status (unified, replaces 3 old tools)
  S991: ci_list_runs, ci_get_run, ci_get_job_logs, ci_search_logs, ci_cancel_run, ci_retry_run
  S992: manage_ssh_key (action), list_ssh_keys, manage_group_members (action), manage_group_repos (action)
  Removed tools: 26 old tool names must NOT silently succeed
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.helpers import _auth_headers, MCP_CALL_TIMEOUT


# ---------------------------------------------------------------------------
# Module-level raw MCP call helper (does NOT assert no-error)
# ---------------------------------------------------------------------------


def _raw_mcp_call(
    client: httpx.Client,
    tool_name: str,
    arguments: dict[str, Any],
    token: str,
) -> tuple[int, dict[str, Any]]:
    """Send MCP tools/call and return (status_code, body_dict) without asserting.

    Used for tests that expect a JSON-RPC-level error (e.g. tool not found)
    where mcp_call() from helpers.py would raise AssertionError.

    Args:
        client: Shared httpx.Client bound to the server base URL.
        tool_name: MCP tool name to call.
        arguments: Tool argument dict.
        token: JWT access token (admin).

    Returns:
        (status_code, body_dict) -- body_dict is {} on non-200 responses.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    resp = client.post(
        "/mcp",
        json=payload,
        headers=_auth_headers(token),
        timeout=MCP_CALL_TIMEOUT,
    )
    if resp.status_code == 200:
        return resp.status_code, resp.json()
    return resp.status_code, {}


def _get_inner(body: dict[str, Any]) -> dict[str, Any]:
    """Extract and decode the inner JSON from result.content[0].text.

    MCP responses wrap handler output as:
      {"result": {"content": [{"type": "text", "text": "{...json...}"}]}}

    Returns the decoded inner dict, or {} on any parse failure.
    """
    result = body.get("result", {})
    content = result.get("content", [])
    if not content:
        return {}
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}


def _assert_no_500(status_code: int, body: dict[str, Any], label: str) -> None:
    """Assert the response is not a 5xx server error."""
    assert status_code < 500, (
        f"{label}: server error HTTP {status_code}, body: {json.dumps(body)[:200]}"
    )


# ---------------------------------------------------------------------------
# S987: cidx_quick_reference + first_time_user_guide
# ---------------------------------------------------------------------------


class TestS987Guides:
    """Story #987: Quick reference tool param and first_time_user_guide."""

    def test_quick_reference_with_tool_param(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """cidx_quick_reference(tool='search_code') returns extended description."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "cidx_quick_reference",
            {"tool": "search_code"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "quick_ref_tool_param")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success, got: {inner}"
        assert inner.get("tool") == "search_code", (
            f"Expected tool='search_code': {inner}"
        )
        assert "body" in inner, f"Expected 'body' in response: {inner}"

    def test_quick_reference_tool_not_found(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """cidx_quick_reference(tool='nonexistent_xyz') returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "cidx_quick_reference",
            {"tool": "nonexistent_xyz_tool_e2e"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "quick_ref_tool_not_found")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for unknown tool, got: {inner}"
        )

    def test_quick_reference_category_filter(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """cidx_quick_reference(category='admin') still works (existing behavior)."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "cidx_quick_reference",
            {"category": "admin"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "quick_ref_category")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success, got: {inner}"

    def test_first_time_user_guide_references_repository_status(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """first_time_user_guide references repository_status, not global_repo_status."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "first_time_user_guide",
            {},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "first_time_guide")
        assert status == 200, f"Expected 200, got {status}"
        # Serialize full response body to check all text content
        raw_text = json.dumps(body)
        assert "repository_status" in raw_text, (
            "Guide should reference 'repository_status' in its step descriptions"
        )
        assert "global_repo_status" not in raw_text, (
            "Guide should NOT reference removed 'global_repo_status'"
        )


# ---------------------------------------------------------------------------
# S989: list_mcp_credentials + manage_mcp_credential
# ---------------------------------------------------------------------------


class TestS989Credentials:
    """Story #989: Consolidated credential tools."""

    def test_list_credentials_scope_self(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """list_mcp_credentials(scope='self') returns success + credentials array."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "list_mcp_credentials",
            {"scope": "self"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "cred_list_self")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success, got: {inner}"
        assert "credentials" in inner, f"Expected 'credentials' key: {inner}"

    def test_list_credentials_scope_all(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """list_mcp_credentials(scope='all') returns success + credentials with usernames."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "list_mcp_credentials",
            {"scope": "all"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "cred_list_all")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success, got: {inner}"
        assert "credentials" in inner, f"Expected 'credentials' key: {inner}"

    def test_list_credentials_scope_system(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """list_mcp_credentials(scope='system') returns success + system_credentials + count."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "list_mcp_credentials",
            {"scope": "system"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "cred_list_system")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success, got: {inner}"
        assert "system_credentials" in inner, f"Expected 'system_credentials': {inner}"
        assert "count" in inner, f"Expected 'count' key: {inner}"

    def test_manage_credential_create_then_delete(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_mcp_credential create+delete lifecycle."""
        desc = f"e2e_epic985_{uuid.uuid4().hex[:8]}"

        # Create
        status_c, body_c = _raw_mcp_call(
            e2e_http_client,
            "manage_mcp_credential",
            {"action": "create", "description": desc},
            e2e_admin_token,
        )
        _assert_no_500(status_c, body_c, "cred_create")
        assert status_c == 200, f"Expected 200 on create, got {status_c}"
        inner_c = _get_inner(body_c)
        assert inner_c.get("success") is True, f"Create failed: {inner_c}"
        cred_id = inner_c.get("credential_id", "")
        assert cred_id, f"Create must return non-empty credential_id: {inner_c}"

        # Delete
        status_d, body_d = _raw_mcp_call(
            e2e_http_client,
            "manage_mcp_credential",
            {"action": "delete", "credential_id": cred_id},
            e2e_admin_token,
        )
        _assert_no_500(status_d, body_d, "cred_delete")
        assert status_d == 200, f"Expected 200 on delete, got {status_d}"
        inner_d = _get_inner(body_d)
        assert inner_d.get("success") is True, f"Delete failed: {inner_d}"

    def test_manage_credential_invalid_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_mcp_credential(action='invalid') returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_mcp_credential",
            {"action": "invalid_e2e"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "cred_invalid_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for invalid action, got: {inner}"
        )

    def test_manage_credential_missing_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_mcp_credential with no action returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_mcp_credential",
            {},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "cred_missing_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for missing action, got: {inner}"
        )


# ---------------------------------------------------------------------------
# S990: repository_status (unified)
# ---------------------------------------------------------------------------


class TestS990RepositoryStatus:
    """Story #990: Unified repository_status tool (replaces 3 old tools)."""

    def test_repository_status_global_basic(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        registered_golden_repo: str,
    ) -> None:
        """repository_status(alias='markupsafe-global') returns kind=global, detail=basic."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "repository_status",
            {"alias": "markupsafe-global"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "repo_status_global_basic")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success: {inner}"
        assert inner.get("kind") == "global", (
            f"Expected kind='global' for markupsafe-global alias: {inner}"
        )
        assert inner.get("detail") == "basic", (
            f"Expected detail='basic' (default): {inner}"
        )

    def test_repository_status_global_stats(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        registered_golden_repo: str,
    ) -> None:
        """repository_status(alias='markupsafe-global', detail='stats') includes statistics."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "repository_status",
            {"alias": "markupsafe-global", "detail": "stats"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "repo_status_global_stats")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success: {inner}"
        assert inner.get("detail") == "stats", (
            f"Expected detail='stats' in response: {inner}"
        )
        assert "statistics" in inner, (
            f"Expected 'statistics' key for stats detail: {inner}"
        )

    def test_repository_status_activated(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        activated_golden_repo: str,
    ) -> None:
        """repository_status(alias='markupsafe') for activated repo returns kind=activated."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "repository_status",
            {"alias": "markupsafe"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "repo_status_activated")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success: {inner}"
        assert inner.get("kind") == "activated", (
            f"Expected kind='activated' for non-global alias: {inner}"
        )

    def test_repository_status_invalid_detail(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        registered_golden_repo: str,
    ) -> None:
        """repository_status(alias='markupsafe-global', detail='bogus') returns validation error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "repository_status",
            {"alias": "markupsafe-global", "detail": "bogus"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "repo_status_bad_detail")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for invalid detail: {inner}"
        )
        assert "detail" in inner.get("error", "").lower(), (
            f"Error message should mention 'detail': {inner}"
        )

    def test_repository_status_missing_alias(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """repository_status with no alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "repository_status",
            {},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "repo_status_no_alias")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for missing alias: {inner}"
        )
        assert "alias" in inner.get("error", "").lower(), (
            f"Error message should mention 'alias': {inner}"
        )


# ---------------------------------------------------------------------------
# S991: Unified CI/CD handlers
# ---------------------------------------------------------------------------


class TestS991CICD:
    """Story #991: Unified CI/CD tools with forge auto-detection."""

    def test_ci_list_runs_with_registered_repo(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        registered_golden_repo: str,
    ) -> None:
        """ci_list_runs(repository_alias='markupsafe-global') returns no 500.

        The live server has full services. Expect success=False with 'no token'
        or a clean error -- but definitely not a 5xx crash.
        """
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_list_runs",
            {"repository_alias": "markupsafe-global"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_list_runs_registered")

    def test_ci_list_runs_nonexistent_repo(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_list_runs with unknown alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_list_runs",
            {"repository_alias": "nonexistent-xyz-e2e-985"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_list_runs_bad_repo")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for unknown repo: {inner}"
        )

    def test_ci_list_runs_invalid_forge(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        registered_golden_repo: str,
    ) -> None:
        """ci_list_runs(forge='invalid') returns validation error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_list_runs",
            {"repository_alias": "markupsafe-global", "forge": "invalid_forge"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_list_runs_bad_forge")
        # Either 200 with success=False, or non-5xx response
        if status == 200:
            inner = _get_inner(body)
            if inner.get("success") is not None:
                assert inner.get("success") is False, (
                    f"Expected success=False for invalid forge: {inner}"
                )

    def test_ci_get_run_clean_error_on_missing_params(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_get_run with no params returns clean error, no 500."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_get_run",
            {},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_get_run_no_params")

    def test_ci_get_run_nonexistent_repo(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_get_run with unknown alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_get_run",
            {"repository_alias": "nonexistent-xyz-e2e-985", "run_id": 1},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_get_run_bad_repo")

    def test_ci_get_job_logs_clean_error(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_get_job_logs with unknown alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_get_job_logs",
            {"repository_alias": "nonexistent-xyz-e2e-985", "job_id": 1},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_get_job_logs_bad")

    def test_ci_search_logs_clean_error(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_search_logs with unknown alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_search_logs",
            {
                "repository_alias": "nonexistent-xyz-e2e-985",
                "run_id": 1,
                "pattern": "error",
            },
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_search_logs_bad")

    def test_ci_cancel_run_clean_error(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_cancel_run with unknown alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_cancel_run",
            {"repository_alias": "nonexistent-xyz-e2e-985", "run_id": 1},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_cancel_run_bad")

    def test_ci_retry_run_clean_error(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """ci_retry_run with unknown alias returns clean error."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "ci_retry_run",
            {"repository_alias": "nonexistent-xyz-e2e-985", "run_id": 1},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ci_retry_run_bad")


# ---------------------------------------------------------------------------
# S992: manage_ssh_key + list_ssh_keys
# ---------------------------------------------------------------------------


class TestS992SSHKeys:
    """Story #992: Consolidated SSH key tools."""

    def test_list_ssh_keys(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """list_ssh_keys() returns success=True with managed/unmanaged arrays."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "list_ssh_keys",
            {},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "list_ssh_keys")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is True, f"Expected success: {inner}"
        assert "managed" in inner, f"Expected 'managed' array: {inner}"

    def test_manage_ssh_key_create_delete_lifecycle(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_ssh_key create then delete full lifecycle."""
        key_name = f"e2e-epic985-{uuid.uuid4().hex[:6]}"

        # Create
        status_c, body_c = _raw_mcp_call(
            e2e_http_client,
            "manage_ssh_key",
            {"action": "create", "name": key_name},
            e2e_admin_token,
        )
        _assert_no_500(status_c, body_c, "ssh_create")
        assert status_c == 200, f"Expected 200 on create, got {status_c}"
        inner_c = _get_inner(body_c)
        assert inner_c.get("success") is True, f"SSH key create failed: {inner_c}"

        # Delete
        status_d, body_d = _raw_mcp_call(
            e2e_http_client,
            "manage_ssh_key",
            {"action": "delete", "name": key_name},
            e2e_admin_token,
        )
        _assert_no_500(status_d, body_d, "ssh_delete")
        assert status_d == 200, f"Expected 200 on delete, got {status_d}"
        inner_d = _get_inner(body_d)
        assert inner_d.get("success") is True, f"SSH key delete failed: {inner_d}"

    def test_manage_ssh_key_show_public_clean_response(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_ssh_key(action='show_public', name='test-key') returns clean response (not 500)."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_ssh_key",
            {"action": "show_public", "name": "e2e-nonexistent-key"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ssh_show_public")
        # Key not found is expected -- but must not be a 5xx
        if status == 200:
            inner = _get_inner(body)
            # Either found (success=True) or not found (success=False) -- both valid
            assert inner.get("success") is not None, (
                f"Response must have 'success' field: {inner}"
            )

    def test_manage_ssh_key_invalid_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_ssh_key(action='invalid') returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_ssh_key",
            {"action": "invalid_e2e"},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ssh_invalid_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for invalid action: {inner}"
        )

    def test_manage_ssh_key_missing_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_ssh_key with no action returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_ssh_key",
            {},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "ssh_missing_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for missing action: {inner}"
        )


# ---------------------------------------------------------------------------
# S992: manage_group_members + manage_group_repos
# ---------------------------------------------------------------------------


class TestS992Groups:
    """Story #992: Consolidated group member/repo management."""

    def test_manage_group_members_add_remove_lifecycle(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """Create group, add member, remove member, delete group -- full lifecycle."""
        grp = f"e2e_epic985_grp_{uuid.uuid4().hex[:6]}"

        # Create group
        create_status, create_body = _raw_mcp_call(
            e2e_http_client,
            "create_group",
            {"name": grp},
            e2e_admin_token,
        )
        _assert_no_500(create_status, create_body, "grp_create")

        try:
            # Add member
            add_status, add_body = _raw_mcp_call(
                e2e_http_client,
                "manage_group_members",
                {"action": "add", "group_id": grp, "users": ["admin"]},
                e2e_admin_token,
            )
            _assert_no_500(add_status, add_body, "grp_add_member")
            if add_status == 200:
                inner = _get_inner(add_body)
                # In live server, group manager should be configured
                # Accept success=True or a descriptive error (not silent failure)
                if inner.get("success") is False:
                    # Error is acceptable but must have a message
                    assert inner.get("error"), (
                        f"Failed add_member must have error message: {inner}"
                    )

            # Remove member
            rm_status, rm_body = _raw_mcp_call(
                e2e_http_client,
                "manage_group_members",
                {"action": "remove", "group_id": grp, "users": ["admin"]},
                e2e_admin_token,
            )
            _assert_no_500(rm_status, rm_body, "grp_remove_member")

        finally:
            # Cleanup: delete group regardless of test outcome
            _raw_mcp_call(
                e2e_http_client,
                "delete_group",
                {"group_name": grp},
                e2e_admin_token,
            )

    def test_manage_group_repos_add_remove_lifecycle(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
        registered_golden_repo: str,
    ) -> None:
        """Create group, add repo, remove repo, delete group -- full lifecycle."""
        grp = f"e2e_epic985_repo_grp_{uuid.uuid4().hex[:6]}"

        create_status, create_body = _raw_mcp_call(
            e2e_http_client,
            "create_group",
            {"name": grp},
            e2e_admin_token,
        )
        _assert_no_500(create_status, create_body, "grp_repo_create")

        try:
            # Add repo
            add_status, add_body = _raw_mcp_call(
                e2e_http_client,
                "manage_group_repos",
                {"action": "add", "group_name": grp, "repos": ["markupsafe"]},
                e2e_admin_token,
            )
            _assert_no_500(add_status, add_body, "grp_add_repo")

            # Remove repo
            rm_status, rm_body = _raw_mcp_call(
                e2e_http_client,
                "manage_group_repos",
                {"action": "remove", "group_name": grp, "repos": ["markupsafe"]},
                e2e_admin_token,
            )
            _assert_no_500(rm_status, rm_body, "grp_remove_repo")

        finally:
            _raw_mcp_call(
                e2e_http_client,
                "delete_group",
                {"group_name": grp},
                e2e_admin_token,
            )

    def test_manage_group_members_invalid_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_group_members(action='invalid') returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_group_members",
            {"action": "invalid_e2e", "group_id": "x", "users": ["admin"]},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "grp_members_invalid_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for invalid action: {inner}"
        )

    def test_manage_group_repos_invalid_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_group_repos(action='invalid') returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_group_repos",
            {"action": "invalid_e2e", "group_name": "x", "repos": ["r"]},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "grp_repos_invalid_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for invalid action: {inner}"
        )

    def test_manage_group_members_missing_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_group_members with no action returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_group_members",
            {"group_id": "x", "users": ["admin"]},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "grp_members_no_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for missing action: {inner}"
        )

    def test_manage_group_repos_missing_action(
        self,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """manage_group_repos with no action returns clean success=False."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            "manage_group_repos",
            {"group_name": "x", "repos": ["r"]},
            e2e_admin_token,
        )
        _assert_no_500(status, body, "grp_repos_no_action")
        assert status == 200, f"Expected 200, got {status}"
        inner = _get_inner(body)
        assert inner.get("success") is False, (
            f"Expected success=False for missing action: {inner}"
        )


# ---------------------------------------------------------------------------
# Removed tool names: must NOT be silently accepted
# ---------------------------------------------------------------------------

_REMOVED_TOOL_NAMES = [
    "create_mcp_credential",
    "delete_mcp_credential",
    "admin_list_user_mcp_credentials",
    "admin_create_user_mcp_credential",
    "admin_delete_user_mcp_credential",
    "admin_list_all_mcp_credentials",
    "admin_list_system_mcp_credentials",
    "add_member_to_group",
    "remove_member_from_group",
    "add_repos_to_group",
    "remove_repo_from_group",
    "bulk_remove_repos_from_group",
    "get_repository_status",
    "get_repository_statistics",
    "global_repo_status",
    "github_actions_list_runs",
    "github_actions_get_run",
    "github_actions_search_logs",
    "github_actions_get_job_logs",
    "github_actions_retry_run",
    "github_actions_cancel_run",
    "cidx_ssh_key_create",
    "cidx_ssh_key_list",
    "cidx_ssh_key_delete",
    "cidx_ssh_key_show_public",
    "cidx_ssh_key_assign_host",
]


class TestRemovedTools:
    """Verify all 26 tool names removed in Epic #985 return JSON-RPC errors.

    These tools must NOT silently succeed (which would mean they are still
    secretly registered in HANDLER_REGISTRY). The expected outcome is either:
    - HTTP 200 with a JSON-RPC error field (tool not found), OR
    - HTTP 4xx (unknown tool dispatch)

    NOT acceptable: HTTP 200 with result.content[0].text JSON having success=True.
    """

    @pytest.mark.parametrize("tool_name", _REMOVED_TOOL_NAMES)
    def test_removed_tool_returns_error(
        self,
        tool_name: str,
        e2e_http_client: httpx.Client,
        e2e_admin_token: str,
    ) -> None:
        """Removed tool must return a JSON-RPC error or a non-success result -- never success=True."""
        status, body = _raw_mcp_call(
            e2e_http_client,
            tool_name,
            {},
            e2e_admin_token,
        )
        # Must not be a 5xx crash
        _assert_no_500(status, body, f"removed_{tool_name}")

        if status == 200:
            # If HTTP 200: must be a JSON-RPC error (tool not found) OR success=False
            has_rpc_error = "error" in body and "result" not in body
            if has_rpc_error:
                # Correct: JSON-RPC level error returned for unknown tool
                return

            # Check if result wraps a handler response with success=False
            if "result" in body:
                content = body["result"].get("content", [])
                if content:
                    text = content[0].get("text", "")
                    try:
                        inner = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        inner = {}
                    assert inner.get("success") is not True, (
                        f"Removed tool '{tool_name}' returned success=True! "
                        "The old tool name is still registered in HANDLER_REGISTRY."
                    )
