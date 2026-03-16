"""MCP route smoke test regression suite (Story #463).

Regression guard against the v9.5.16 bug where browse_directory, get_file_content,
and list_files crashed with "module 'code_indexer.server.app' has no attribute 'file_service'".

Every MCP tool that accesses app.py module globals must be invokable without crashing
with AttributeError, NameError, ModuleNotFoundError, ImportError, or TypeError.

Scope: All MCP tools that access app_module.X globals in handlers.py.
Excluded: Write-mode tools (create_file, edit_file, delete_file, enter_write_mode,
exit_write_mode) require active repository setup that is out of scope for smoke tests.
Git operation tools (git_commit, git_push, etc.) are excluded for the same reason.
"""

import json

import pytest
from fastapi.testclient import TestClient

# Crash indicators that signal a module-attribute regression (not legitimate business errors)
CRASH_INDICATORS = [
    "NameError",
    "AttributeError",
    "ModuleNotFoundError",
    "ImportError",
    "has no attribute",
    "is not defined",
    "cannot import name",
    "TypeError: 'NoneType'",
]


def _assert_no_crash(resp, tool_name: str) -> str:
    """Assert the MCP response does not contain crash indicators.

    Returns the response text for optional further inspection.
    A legitimate 'not found' or 'unauthorized' error is acceptable;
    a crash (AttributeError, NameError, etc.) is not.
    """
    assert resp.status_code == 200, f"{tool_name}: HTTP {resp.status_code}"
    body = resp.json()
    if "result" in body:
        result = body["result"]
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            text = content[0].get("text", "") if content else ""
        else:
            text = str(result)
    elif "error" in body:
        text = str(body["error"])
    else:
        text = str(body)

    for indicator in CRASH_INDICATORS:
        assert (
            indicator not in text
        ), f"{tool_name} CRASHED with '{indicator}': {text[:400]}"
    return text


def _mcp_call(client: TestClient, token: str, tool_name: str, arguments: dict):
    """Execute an MCP JSON-RPC tools/call request."""
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# Fixtures
# Module scope is safe because entire suite runs in <30 seconds (JWT TTL=10min)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    """In-process FastAPI TestClient -- no external server required."""
    from code_indexer.server.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def admin_token(client):
    """Authenticate as admin and return JWT access token."""
    resp = client.post(
        "/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert (
        resp.status_code == 200
    ), f"Login failed: {resp.status_code} {resp.text[:200]}"
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Suite 1: Verify app module globals exist and are not None
# ---------------------------------------------------------------------------

REQUIRED_ATTRIBUTES = [
    "golden_repo_manager",
    "activated_repo_manager",
    "user_manager",
    "file_service",
    "background_job_manager",
    "semantic_query_manager",
    "repository_listing_manager",
]


class TestAppModuleAttributesExist:
    """Verify all required module-level globals are present and initialized."""

    @pytest.mark.parametrize("attr", REQUIRED_ATTRIBUTES)
    def test_attribute_exists_and_not_none(self, attr):
        from code_indexer.server import app as app_module

        assert hasattr(app_module, attr), f"app module missing attribute: {attr}"
        assert (
            getattr(app_module, attr) is not None
        ), f"app module attribute is None: {attr}"


# ---------------------------------------------------------------------------
# Suite 2: Smoke-test every MCP tool that touches app.py globals
# ---------------------------------------------------------------------------


class TestMcpRouteSmoke:
    """Smoke test every MCP tool that accesses app.py module globals.

    These tests do NOT assert business-correct results -- they assert that
    the route handler does not crash with a module-attribute exception.
    Legitimate 'not found', 'permission denied', or 'invalid argument'
    responses are acceptable outcomes.
    """

    # ------------------------------------------------------------------
    # file_service tools (3 tools -- v9.5.16 regression point)
    # ------------------------------------------------------------------

    def test_browse_directory_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "browse_directory", {"path": "/"})
        _assert_no_crash(resp, "browse_directory")

    def test_get_file_content_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "get_file_content",
            {"file_path": "/nonexistent/file.py"},
        )
        _assert_no_crash(resp, "get_file_content")

    def test_list_files_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "list_files",
            {"repository_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "list_files")

    # ------------------------------------------------------------------
    # golden_repo_manager tools (11 tools)
    # ------------------------------------------------------------------

    def test_search_code_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "search_code",
            {"query_text": "test query", "repository_alias": "nonexistent"},
        )
        _assert_no_crash(resp, "search_code")

    def test_regex_search_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "regex_search",
            {"pattern": "test.*", "repository_alias": "nonexistent"},
        )
        _assert_no_crash(resp, "regex_search")

    def test_list_repositories_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "list_repositories", {})
        _assert_no_crash(resp, "list_repositories")

    def test_discover_repositories_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "discover_repositories", {})
        _assert_no_crash(resp, "discover_repositories")

    def test_list_repo_categories_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "list_repo_categories", {})
        _assert_no_crash(resp, "list_repo_categories")

    def test_get_repository_status_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "get_repository_status",
            {"repository_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "get_repository_status")

    def test_get_repository_statistics_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "get_repository_statistics",
            {"repository_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "get_repository_statistics")

    def test_check_hnsw_health_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "check_hnsw_health", {})
        _assert_no_crash(resp, "check_hnsw_health")

    def test_add_golden_repo_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "add_golden_repo",
            {
                "url": "https://github.com/nonexistent/repo.git",
                "alias": "smoke-test-repo",
                "description": "Smoke test repo",
            },
        )
        _assert_no_crash(resp, "add_golden_repo")

    def test_remove_golden_repo_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "remove_golden_repo",
            {"alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "remove_golden_repo")

    def test_refresh_golden_repo_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "refresh_golden_repo",
            {"alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "refresh_golden_repo")

    def test_change_golden_repo_branch_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "change_golden_repo_branch",
            {"alias": "nonexistent-repo", "branch": "main"},
        )
        _assert_no_crash(resp, "change_golden_repo_branch")

    def test_cidx_quick_reference_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "cidx_quick_reference", {})
        _assert_no_crash(resp, "cidx_quick_reference")

    # ------------------------------------------------------------------
    # activated_repo_manager tools (7 tools)
    # ------------------------------------------------------------------

    def test_activate_repository_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "activate_repository",
            {"golden_repo_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "activate_repository")

    def test_deactivate_repository_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "deactivate_repository",
            {"user_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "deactivate_repository")

    def test_sync_repository_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "sync_repository",
            {"user_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "sync_repository")

    def test_switch_branch_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "switch_branch",
            {"user_alias": "nonexistent-repo", "branch_name": "main"},
        )
        _assert_no_crash(resp, "switch_branch")

    def test_get_branches_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "get_branches",
            {"repository_alias": "nonexistent-repo"},
        )
        _assert_no_crash(resp, "get_branches")

    def test_get_all_repositories_status_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "get_all_repositories_status", {})
        _assert_no_crash(resp, "get_all_repositories_status")

    def test_manage_composite_repository_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "manage_composite_repository",
            {"operation": "list", "user_alias": "nonexistent-composite"},
        )
        _assert_no_crash(resp, "manage_composite_repository")

    # ------------------------------------------------------------------
    # background_job_manager tools (2 tools)
    # ------------------------------------------------------------------

    def test_get_job_statistics_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "get_job_statistics", {})
        _assert_no_crash(resp, "get_job_statistics")

    def test_get_job_details_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "get_job_details",
            {"job_id": "nonexistent-job-id"},
        )
        _assert_no_crash(resp, "get_job_details")

    # ------------------------------------------------------------------
    # user_manager tools (6 tools)
    # ------------------------------------------------------------------

    def test_list_users_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "list_users", {})
        _assert_no_crash(resp, "list_users")

    def test_create_user_no_crash(self, client, admin_token):
        # Use existing "admin" username — will return "already exists" error
        # without creating anything. No side effects.
        resp = _mcp_call(
            client,
            admin_token,
            "create_user",
            {
                "username": "admin",
                "password": "SmokeTestPass123!",
                "role": "normal_user",
            },
        )
        _assert_no_crash(resp, "create_user")

    def test_set_session_impersonation_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "set_session_impersonation",
            {"target_username": "nonexistent-user"},
        )
        _assert_no_crash(resp, "set_session_impersonation")

    def test_list_api_keys_no_crash(self, client, admin_token):
        resp = _mcp_call(client, admin_token, "list_api_keys", {})
        _assert_no_crash(resp, "list_api_keys")

    def test_create_api_key_no_crash(self, client, admin_token):
        # Create key, then immediately clean up to avoid leaked state
        resp = _mcp_call(
            client,
            admin_token,
            "create_api_key",
            {"name": "smoke-test-key-463"},
        )
        text = _assert_no_crash(resp, "create_api_key")
        # Best-effort cleanup: extract key_id and delete it
        try:
            data = json.loads(text)
            key_id = data.get("key_id") or data.get("api_key", {}).get("key_id")
            if key_id:
                _mcp_call(client, admin_token, "delete_api_key", {"key_id": key_id})
        except Exception:
            pass  # Cleanup failure is non-fatal for smoke test

    def test_delete_api_key_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "delete_api_key",
            {"key_id": "nonexistent-key-id"},
        )
        _assert_no_crash(resp, "delete_api_key")

    # ------------------------------------------------------------------
    # app.state tools (PayloadCache access pattern)
    # ------------------------------------------------------------------

    def test_get_cached_content_no_crash(self, client, admin_token):
        resp = _mcp_call(
            client,
            admin_token,
            "get_cached_content",
            {"handle": "00000000-0000-0000-0000-000000000000"},
        )
        _assert_no_crash(resp, "get_cached_content")
