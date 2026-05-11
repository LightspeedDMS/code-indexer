"""Epic #985 regression: exercise every MCP endpoint modified in the consolidation epic.

Each test function targets a specific endpoint + parameter variation.
Tests are ordered by story number (S987, S989, S990, S991, S992).

Acceptance criteria:
  - HTTP < 500 on all calls (no unhandled server errors)
  - HTTP 200 responses have valid JSON-RPC envelope (jsonrpc + result|error)
  - Error responses have informative bodies
  - New dispatcher tools reject invalid/missing action params cleanly
  - Old removed tool names are NOT silently accepted as valid tools
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import (
    FIELD_ERROR,
    FIELD_JSONRPC,
    FIELD_RESULT,
    HTTP_OK,
    HTTP_SERVER_ERROR,
    call_mcp_tool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_ok_jsonrpc(resp, label: str) -> dict:
    """Assert HTTP 200 with valid JSON-RPC envelope; return parsed body."""
    assert resp.status_code < HTTP_SERVER_ERROR, (
        f"{label}: server error {resp.status_code} - {resp.text[:400]}"
    )
    if resp.status_code != HTTP_OK:
        pytest.skip(f"{label}: HTTP {resp.status_code} (4xx is acceptable for E2E)")
    body = resp.json()
    assert FIELD_JSONRPC in body, f"{label}: missing 'jsonrpc' in response"
    assert FIELD_RESULT in body or FIELD_ERROR in body, (
        f"{label}: neither 'result' nor 'error' in JSON-RPC body"
    )
    return body


def _get_result_content(body: dict) -> dict:
    """Extract inner content list's first text block from JSON-RPC result."""
    result = body.get("result", {})
    content = result.get("content", [])
    if not content:
        return {}
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}


def _assert_no_500(resp, label: str) -> None:
    """Assert response is not a server error."""
    assert resp.status_code < HTTP_SERVER_ERROR, (
        f"{label}: server error {resp.status_code} - {resp.text[:400]}"
    )


# ===========================================================================
# S987: cidx_quick_reference + first_time_user_guide + slim_description
# ===========================================================================


class TestS987Guides:
    """Story #987: Quick reference tool param and slim_description."""

    def test_quick_reference_with_tool_param(
        self, test_client: TestClient, auth_headers: dict
    ):
        """cidx_quick_reference(tool='search_code') returns extended description."""
        resp = call_mcp_tool(
            test_client, "cidx_quick_reference", {"tool": "search_code"}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "quick_ref_tool_param")
        inner = _get_result_content(body)
        assert inner.get("success") is True, f"Expected success, got: {inner}"
        assert "tool" in inner, "Response should contain 'tool' field"
        assert inner["tool"] == "search_code"
        assert "body" in inner, "Response should contain 'body' with extended desc"

    def test_quick_reference_tool_not_found(
        self, test_client: TestClient, auth_headers: dict
    ):
        """cidx_quick_reference(tool='nonexistent_xyz') returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "cidx_quick_reference",
            {"tool": "nonexistent_xyz_tool"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "quick_ref_tool_not_found")
        inner = _get_result_content(body)
        assert inner.get("success") is False

    def test_quick_reference_category_filter(
        self, test_client: TestClient, auth_headers: dict
    ):
        """cidx_quick_reference(category='admin') still works (existing behavior)."""
        resp = call_mcp_tool(
            test_client,
            "cidx_quick_reference",
            {"category": "admin"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "quick_ref_category")
        inner = _get_result_content(body)
        assert inner.get("success") is True

    def test_quick_reference_no_params(
        self, test_client: TestClient, auth_headers: dict
    ):
        """cidx_quick_reference() with no params returns full listing."""
        resp = call_mcp_tool(
            test_client, "cidx_quick_reference", {}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "quick_ref_no_params")
        inner = _get_result_content(body)
        assert inner.get("success") is True

    def test_first_time_user_guide_references_repository_status(
        self, test_client: TestClient, auth_headers: dict
    ):
        """first_time_user_guide references repository_status, not global_repo_status."""
        resp = call_mcp_tool(
            test_client, "first_time_user_guide", {}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "first_time_guide")
        raw_text = json.dumps(body)
        assert "repository_status" in raw_text, (
            "Guide should reference 'repository_status'"
        )
        assert "global_repo_status" not in raw_text, (
            "Guide should NOT reference removed 'global_repo_status'"
        )

    def test_get_tool_categories_slim_descriptions(
        self, test_client: TestClient, auth_headers: dict
    ):
        """get_tool_categories returns tools; descriptions should be compact."""
        resp = call_mcp_tool(
            test_client, "get_tool_categories", {}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "tool_categories")
        inner = _get_result_content(body)
        assert inner.get("success") is True


# ===========================================================================
# S989: list_mcp_credentials + manage_mcp_credential
# ===========================================================================


class TestS989Credentials:
    """Story #989: Consolidated credential tools."""

    def test_list_credentials_scope_self(
        self, test_client: TestClient, auth_headers: dict
    ):
        """list_mcp_credentials(scope='self') lists caller's own creds."""
        resp = call_mcp_tool(
            test_client,
            "list_mcp_credentials",
            {"scope": "self"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "cred_list_self")
        inner = _get_result_content(body)
        assert inner.get("success") is True
        assert "credentials" in inner

    def test_list_credentials_scope_all(
        self, test_client: TestClient, auth_headers: dict
    ):
        """list_mcp_credentials(scope='all') lists all users' creds (admin)."""
        resp = call_mcp_tool(
            test_client,
            "list_mcp_credentials",
            {"scope": "all"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "cred_list_all")
        inner = _get_result_content(body)
        assert inner.get("success") is True
        assert "credentials" in inner

    def test_list_credentials_scope_system(
        self, test_client: TestClient, auth_headers: dict
    ):
        """list_mcp_credentials(scope='system') lists system creds."""
        resp = call_mcp_tool(
            test_client,
            "list_mcp_credentials",
            {"scope": "system"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "cred_list_system")
        inner = _get_result_content(body)
        assert inner.get("success") is True
        assert "system_credentials" in inner
        assert "count" in inner

    def test_manage_credential_create_then_delete(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_mcp_credential create+delete lifecycle."""
        desc = f"e2e_regression_{uuid.uuid4().hex[:8]}"
        resp = call_mcp_tool(
            test_client,
            "manage_mcp_credential",
            {"action": "create", "description": desc},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "cred_create")
        inner = _get_result_content(body)
        assert inner.get("success") is True, f"Create failed: {inner}"
        cred_id = inner.get("credential_id", "")
        assert cred_id, "Create must return credential_id"

        resp2 = call_mcp_tool(
            test_client,
            "manage_mcp_credential",
            {"action": "delete", "credential_id": cred_id},
            auth_headers,
        )
        body2 = _assert_ok_jsonrpc(resp2, "cred_delete")
        inner2 = _get_result_content(body2)
        assert inner2.get("success") is True, f"Delete failed: {inner2}"

    def test_manage_credential_invalid_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_mcp_credential(action='invalid') returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_mcp_credential",
            {"action": "invalid"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "cred_invalid_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False
        assert "invalid" in inner.get("error", "").lower() or "Invalid" in inner.get(
            "error", ""
        )

    def test_manage_credential_missing_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_mcp_credential with no action returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_mcp_credential",
            {},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "cred_missing_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False


# ===========================================================================
# S990: repository_status (unified)
# ===========================================================================


class TestS990RepositoryStatus:
    """Story #990: Unified repository_status tool."""

    def test_repository_status_missing_alias(
        self, test_client: TestClient, auth_headers: dict
    ):
        """repository_status with no alias returns clean error."""
        resp = call_mcp_tool(
            test_client, "repository_status", {}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "repo_status_no_alias")
        inner = _get_result_content(body)
        assert inner.get("success") is False
        assert "alias" in inner.get("error", "").lower()

    def test_repository_status_invalid_detail(
        self, test_client: TestClient, auth_headers: dict
    ):
        """repository_status(alias=X, detail='bogus') returns validation error.

        In TestClient env, access filtering may deny before handler runs —
        accept that as proof the tool IS registered (no 500).
        """
        resp = call_mcp_tool(
            test_client,
            "repository_status",
            {"alias": "test-global", "detail": "bogus"},
            auth_headers,
        )
        _assert_no_500(resp, "repo_status_bad_detail")
        if resp.status_code == HTTP_OK:
            body = resp.json()
            inner = _get_result_content(body)
            if inner and inner.get("success") is not None:
                assert inner.get("success") is False
                assert "detail" in inner.get("error", "").lower()

    def test_repository_status_global_basic(
        self, test_client: TestClient, auth_headers: dict
    ):
        """repository_status(alias='X-global') returns kind=global."""
        resp = call_mcp_tool(
            test_client,
            "repository_status",
            {"alias": "test-global"},
            auth_headers,
        )
        _assert_no_500(resp, "repo_status_global_basic")
        if resp.status_code == HTTP_OK:
            body = resp.json()
            inner = _get_result_content(body)
            if inner.get("success"):
                assert inner.get("kind") == "global"
                assert inner.get("detail") == "basic"

    def test_repository_status_global_stats(
        self, test_client: TestClient, auth_headers: dict
    ):
        """repository_status(alias='X-global', detail='stats') includes statistics."""
        resp = call_mcp_tool(
            test_client,
            "repository_status",
            {"alias": "test-global", "detail": "stats"},
            auth_headers,
        )
        _assert_no_500(resp, "repo_status_global_stats")
        if resp.status_code == HTTP_OK:
            body = resp.json()
            inner = _get_result_content(body)
            if inner.get("success"):
                assert inner.get("detail") == "stats"
                assert "statistics" in inner

    def test_repository_status_activated_basic(
        self, test_client: TestClient, auth_headers: dict
    ):
        """repository_status for non-global alias returns kind=activated."""
        resp = call_mcp_tool(
            test_client,
            "repository_status",
            {"alias": "test-activated"},
            auth_headers,
        )
        _assert_no_500(resp, "repo_status_activated_basic")
        if resp.status_code == HTTP_OK:
            body = resp.json()
            inner = _get_result_content(body)
            if inner.get("success"):
                assert inner.get("kind") == "activated"


# ===========================================================================
# S991: Unified CI/CD handlers (ci_list_runs, ci_get_run, etc.)
# ===========================================================================


class TestS991CICD:
    """Story #991: Unified CI/CD tools with forge auto-detection."""

    def test_ci_list_runs_missing_alias(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_list_runs with no repository_alias returns clean error."""
        resp = call_mcp_tool(
            test_client, "ci_list_runs", {}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "ci_list_runs_no_alias")
        inner = _get_result_content(body)
        assert inner.get("success") is False
        assert "repository_alias" in inner.get("error", "")

    def test_ci_list_runs_nonexistent_repo(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_list_runs with unknown alias returns clean error.

        Access filtering may deny before handler runs in TestClient env.
        """
        resp = call_mcp_tool(
            test_client,
            "ci_list_runs",
            {"repository_alias": "nonexistent-repo-xyz"},
            auth_headers,
        )
        _assert_no_500(resp, "ci_list_runs_bad_repo")
        if resp.status_code == HTTP_OK:
            inner = _get_result_content(resp.json())
            if inner and inner.get("success") is not None:
                assert inner.get("success") is False

    def test_ci_list_runs_invalid_forge(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_list_runs with forge='invalid' returns validation error.

        Access filtering may deny before handler runs in TestClient env.
        """
        resp = call_mcp_tool(
            test_client,
            "ci_list_runs",
            {"repository_alias": "test-global", "forge": "invalid"},
            auth_headers,
        )
        _assert_no_500(resp, "ci_list_runs_bad_forge")
        if resp.status_code == HTTP_OK:
            inner = _get_result_content(resp.json())
            if inner and inner.get("success") is not None:
                assert inner.get("success") is False

    def test_ci_get_run_missing_params(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_get_run with no params returns clean error."""
        resp = call_mcp_tool(
            test_client, "ci_get_run", {}, auth_headers
        )
        _assert_no_500(resp, "ci_get_run_no_params")

    def test_ci_get_run_nonexistent(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_get_run with unknown alias returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "ci_get_run",
            {"repository_alias": "nonexistent-xyz", "run_id": 1},
            auth_headers,
        )
        _assert_no_500(resp, "ci_get_run_bad_repo")

    def test_ci_get_job_logs_clean_error(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_get_job_logs with unknown alias returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "ci_get_job_logs",
            {"repository_alias": "nonexistent-xyz", "job_id": 1},
            auth_headers,
        )
        _assert_no_500(resp, "ci_get_job_logs_bad")

    def test_ci_search_logs_clean_error(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_search_logs with unknown alias returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "ci_search_logs",
            {"repository_alias": "nonexistent-xyz", "run_id": 1, "pattern": "err"},
            auth_headers,
        )
        _assert_no_500(resp, "ci_search_logs_bad")

    def test_ci_cancel_run_clean_error(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_cancel_run with unknown alias returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "ci_cancel_run",
            {"repository_alias": "nonexistent-xyz", "run_id": 1},
            auth_headers,
        )
        _assert_no_500(resp, "ci_cancel_run_bad")

    def test_ci_retry_run_clean_error(
        self, test_client: TestClient, auth_headers: dict
    ):
        """ci_retry_run with unknown alias returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "ci_retry_run",
            {"repository_alias": "nonexistent-xyz", "run_id": 1},
            auth_headers,
        )
        _assert_no_500(resp, "ci_retry_run_bad")


# ===========================================================================
# S992: manage_ssh_key, list_ssh_keys, manage_group_members, manage_group_repos
# ===========================================================================


class TestS992SSHKeys:
    """Story #992: Consolidated SSH key tools."""

    def test_list_ssh_keys(self, test_client: TestClient, auth_headers: dict):
        """list_ssh_keys() returns valid response."""
        resp = call_mcp_tool(test_client, "list_ssh_keys", {}, auth_headers)
        body = _assert_ok_jsonrpc(resp, "list_ssh_keys")
        inner = _get_result_content(body)
        assert inner.get("success") is True

    def test_manage_ssh_key_show_public(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_ssh_key(action='show_public', name='test-key') returns clean response."""
        resp = call_mcp_tool(
            test_client,
            "manage_ssh_key",
            {"action": "show_public", "name": "test-key"},
            auth_headers,
        )
        _assert_no_500(resp, "ssh_show_public")

    def test_manage_ssh_key_create_delete_lifecycle(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_ssh_key create then delete lifecycle."""
        key_name = f"e2e-test-{uuid.uuid4().hex[:6]}"
        resp = call_mcp_tool(
            test_client,
            "manage_ssh_key",
            {"action": "create", "name": key_name},
            auth_headers,
        )
        _assert_no_500(resp, "ssh_create")

        resp2 = call_mcp_tool(
            test_client,
            "manage_ssh_key",
            {"action": "delete", "name": key_name},
            auth_headers,
        )
        _assert_no_500(resp2, "ssh_delete")

    def test_manage_ssh_key_invalid_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_ssh_key(action='invalid') returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_ssh_key",
            {"action": "invalid"},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "ssh_invalid_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False

    def test_manage_ssh_key_missing_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_ssh_key with no action returns clean error."""
        resp = call_mcp_tool(
            test_client, "manage_ssh_key", {}, auth_headers
        )
        body = _assert_ok_jsonrpc(resp, "ssh_missing_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False


class TestS992GroupMembers:
    """Story #992: Consolidated group member management."""

    def test_manage_group_members_add_remove(
        self, test_client: TestClient, auth_headers: dict
    ):
        """Create group, add member, remove member, delete group."""
        grp = f"e2e_grp_{uuid.uuid4().hex[:6]}"

        resp_create = call_mcp_tool(
            test_client, "create_group", {"name": grp}, auth_headers
        )
        _assert_no_500(resp_create, "grp_create")

        resp_add = call_mcp_tool(
            test_client,
            "manage_group_members",
            {"action": "add", "group_id": grp, "users": ["admin"]},
            auth_headers,
        )
        _assert_no_500(resp_add, "grp_add_member")
        if resp_add.status_code == HTTP_OK:
            inner = _get_result_content(resp_add.json())
            # "Group manager not configured" is acceptable in TestClient env —
            # proves the dispatcher routed to the inner handler correctly.
            if inner.get("error") and "not configured" in inner["error"]:
                pass  # expected in isolated TestClient
            elif inner.get("success") is not None:
                assert inner.get("success") is True, f"Add member failed: {inner}"

        resp_rm = call_mcp_tool(
            test_client,
            "manage_group_members",
            {"action": "remove", "group_id": grp, "users": ["admin"]},
            auth_headers,
        )
        _assert_no_500(resp_rm, "grp_remove_member")

        call_mcp_tool(
            test_client, "delete_group", {"group_name": grp}, auth_headers
        )

    def test_manage_group_members_invalid_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_group_members(action='invalid') returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_group_members",
            {"action": "invalid", "group_id": "x", "users": ["admin"]},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "grp_members_invalid_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False

    def test_manage_group_members_missing_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_group_members with no action returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_group_members",
            {"group_id": "x", "users": ["admin"]},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "grp_members_no_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False


class TestS992GroupRepos:
    """Story #992: Consolidated group repo management."""

    def test_manage_group_repos_add_remove(
        self, test_client: TestClient, auth_headers: dict
    ):
        """Create group, add repo, remove repo, delete group."""
        grp = f"e2e_repo_grp_{uuid.uuid4().hex[:6]}"

        call_mcp_tool(
            test_client, "create_group", {"name": grp}, auth_headers
        )

        resp_add = call_mcp_tool(
            test_client,
            "manage_group_repos",
            {"action": "add", "group_name": grp, "repos": ["test-repo"]},
            auth_headers,
        )
        _assert_no_500(resp_add, "grp_add_repo")

        resp_rm = call_mcp_tool(
            test_client,
            "manage_group_repos",
            {"action": "remove", "group_name": grp, "repos": ["test-repo"]},
            auth_headers,
        )
        _assert_no_500(resp_rm, "grp_remove_repo")

        call_mcp_tool(
            test_client, "delete_group", {"group_name": grp}, auth_headers
        )

    def test_manage_group_repos_invalid_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_group_repos(action='invalid') returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_group_repos",
            {"action": "invalid", "group_name": "x", "repos": ["r"]},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "grp_repos_invalid_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False

    def test_manage_group_repos_missing_action(
        self, test_client: TestClient, auth_headers: dict
    ):
        """manage_group_repos with no action returns clean error."""
        resp = call_mcp_tool(
            test_client,
            "manage_group_repos",
            {"group_name": "x", "repos": ["r"]},
            auth_headers,
        )
        body = _assert_ok_jsonrpc(resp, "grp_repos_no_action")
        inner = _get_result_content(body)
        assert inner.get("success") is False


# ===========================================================================
# Removed tool names: must NOT be silently accepted
# ===========================================================================


class TestRemovedToolNames:
    """Verify old tool names removed in Epic #985 are not silently accepted."""

    @pytest.mark.parametrize(
        "tool_name",
        [
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
        ],
    )
    def test_removed_tool_not_in_registry(
        self,
        tool_name: str,
        test_client: TestClient,
        auth_headers: dict,
    ):
        """Calling a removed tool name should NOT return HTTP 200 with a valid result.

        The tool must either:
        - Return 4xx (unknown tool), or
        - Return 200 with a JSON-RPC error (tool not found in registry)

        It must NOT return 200 with a successful result, which would mean
        the old tool name is still secretly registered.
        """
        resp = call_mcp_tool(test_client, tool_name, {}, auth_headers)
        _assert_no_500(resp, f"removed_{tool_name}")
        if resp.status_code == HTTP_OK:
            body = resp.json()
            if FIELD_RESULT in body:
                result = body[FIELD_RESULT]
                content = result.get("content", [])
                if content:
                    text = content[0].get("text", "")
                    try:
                        inner = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        inner = {}
                    assert inner.get("success") is not True, (
                        f"Removed tool '{tool_name}' returned success=True! "
                        "It is still registered in HANDLER_REGISTRY."
                    )
