"""
Phase 3 — Destructive MCP commands (runs LAST in Phase 3).

These tests exercise destructive MCP operations that must run after all
other Phase 3 tests have completed.  File prefix ``test_99_`` and test
prefix ``test_zzz_`` together guarantee alphabetical ordering places every
test here after all preceding Phase 3 test files.

Each test creates a dedicated throwaway resource (with an ``E2E_DESTROY_``
prefix), verifies actual resource state, then deletes it.

Design rules:
  - NEVER delete the ``admin`` user or ``admin`` group.
  - HTTP 200 with valid JSON-RPC shape = success (validated for ALL 200 responses).
  - Clean 4xx acceptable for delete operations (already-gone is fine).
  - HTTP 5xx always fails.
  - Resources carry the ``E2E_DESTROY_`` prefix to distinguish them from the
    ``e2e_`` resources created by test_06_mcp_admin.py.

API signature facts (verified from handler source):
  - create_api_key: takes ``description``; returns ``key_id``
  - delete_api_key: takes ``key_id``
  - create_mcp_credential: takes ``description``; returns ``credential_id``
  - delete_mcp_credential: takes ``credential_id``
  - list_api_keys: ``keys`` list with ``id`` and ``description`` per entry
  - list_mcp_credentials: ``credentials`` list with ``id`` and ``description`` per entry
  - delete_user: NOT in MCP registry; user test creates + verifies only
  - get_maintenance_status: returns ``in_maintenance`` boolean field

Destructive operations covered:
  1. Create + verify + delete a throwaway group.
  2. Create + verify + delete a throwaway API key.
  3. Create + verify + delete a throwaway MCP credential.
  4. Create + verify a throwaway user (no MCP delete_user tool exists).
  5. Enter maintenance mode → verify in_maintenance=True → exit.
  6. Cleanup sweep: delete lingering E2E_DESTROY_ group (name-based),
     API keys (list → filter by description prefix → delete by id),
     and MCP credentials (list → filter by description prefix → delete by id).

Total: 6 test cases.
"""

from __future__ import annotations

import json as _json
import secrets
import string
from typing import cast

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import (
    FIELD_ERROR,
    FIELD_JSONRPC,
    FIELD_RESULT,
    HTTP_OK,
    HTTP_SERVER_ERROR,
    MAX_ERROR_SNIPPET,
    call_mcp_tool,
)

# ---------------------------------------------------------------------------
# Test password generator
# ---------------------------------------------------------------------------


def _make_test_password() -> str:
    """Generate a random password that satisfies the server's complexity policy.

    Policy requirements (from server validation error messages):
      - At least one uppercase letter
      - At least one lowercase letter
      - At least one digit
      - At least one special character

    Uses stdlib ``secrets`` for randomness and ``string`` for character sets.
    No password literals are embedded in source.
    """
    upper = secrets.choice(string.ascii_uppercase)
    lower = secrets.choice(string.ascii_lowercase)
    digit = secrets.choice(string.digits)
    special = secrets.choice("!@#$%^&*")
    rest = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(8)
    )
    chars = list(upper + lower + digit + special + rest)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# ---------------------------------------------------------------------------
# Resource identifiers
# ---------------------------------------------------------------------------
_DESTROY_GROUP: str = "E2E_DESTROY_grp"
_DESTROY_API_KEY_DESC: str = "E2E_DESTROY_key"
_DESTROY_MCP_CRED_DESC: str = "E2E_DESTROY_cred"
_DESTROY_USERNAME: str = "E2E_DESTROY_user"
_DESTROY_USER_PASSWORD: str = _make_test_password()
_DESTROY_PREFIX: str = "E2E_DESTROY_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_resp(resp, *, label: str, allow_4xx: bool = False) -> None:
    """Assert MCP response meets the declared response contract.

    For ALL responses:
      - HTTP 5xx always fails.
      - HTTP 200 always requires a valid JSON-RPC 2.0 shape.

    If allow_4xx is False (default):
      - HTTP 200 is the only acceptable status.

    If allow_4xx is True (delete operations):
      - HTTP 4xx is also acceptable; such responses must have a non-empty body.
    """
    assert resp.status_code < HTTP_SERVER_ERROR, (
        f"{label}: server error {resp.status_code} — {resp.text[:MAX_ERROR_SNIPPET]}"
    )
    if resp.status_code == HTTP_OK:
        body = resp.json()
        assert FIELD_JSONRPC in body, f"{label}: missing {FIELD_JSONRPC!r} key"
        assert FIELD_RESULT in body or FIELD_ERROR in body, (
            f"{label}: JSON-RPC response missing both "
            f"{FIELD_RESULT!r} and {FIELD_ERROR!r}"
        )
    else:
        assert allow_4xx, (
            f"{label}: expected HTTP 200, got {resp.status_code} — "
            f"{resp.text[:MAX_ERROR_SNIPPET]}"
        )
        assert resp.text, f"{label}: HTTP {resp.status_code} response has empty body"


def _mcp_result(resp) -> dict:
    """Unwrap the handler payload from a 200 JSON-RPC response.

    MCP handlers wrap their return dict in ``result.content[0].text`` as a
    JSON string.  Returns the unwrapped dict, or the raw result if absent.
    """
    body = resp.json()
    result = body.get(FIELD_RESULT, {})
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and "text" in first:
                return cast(
                    dict, _json.loads(first["text"])
                )  # json.loads returns Any; payload is always a dict
    return cast(dict, result)  # resp.json() returns Any; result is always a dict


def _sweep_by_description(
    client: TestClient,
    headers: dict,
    *,
    list_tool: str,
    collection_field: str,
    id_field: str,
    delete_tool: str,
    delete_arg: str,
) -> None:
    """List a resource collection and delete entries with an E2E_DESTROY_ description.

    The list call must succeed (HTTP 200 with valid JSON-RPC shape).
    Each matching delete call is routed through _assert_resp(allow_4xx=True)
    so 200 responses are validated and only clean 4xx deletes are tolerated.

    Args:
        client: Session-scoped TestClient.
        headers: Authorization headers.
        list_tool: MCP tool name for listing (e.g. ``list_api_keys``).
        collection_field: Payload key holding the list (e.g. ``keys``).
        id_field: Per-entry key holding the resource ID (e.g. ``id``).
        delete_tool: MCP tool name for deletion (e.g. ``delete_api_key``).
        delete_arg: Argument name the delete tool expects (e.g. ``key_id``).
    """
    list_resp = call_mcp_tool(client, list_tool, {}, headers)
    _assert_resp(list_resp, label=f"sweep {list_tool}")
    for entry in _mcp_result(list_resp).get(collection_field, []):
        if entry.get("description", "").startswith(_DESTROY_PREFIX):
            resource_id = entry.get(id_field, "")
            if resource_id:
                _assert_resp(
                    call_mcp_tool(
                        client, delete_tool, {delete_arg: resource_id}, headers
                    ),
                    label=f"sweep {delete_tool} {resource_id!r}",
                    allow_4xx=True,
                )


# ---------------------------------------------------------------------------
# Destructive MCP tests — test_zzz_ prefix guarantees last execution order
# ---------------------------------------------------------------------------

# Error message substring returned by create_group when the group manager
# feature is not enabled in the server instance.
_GROUP_MANAGER_NOT_CONFIGURED: str = "not configured"

# Error code returned by MCP tools decorated with @require_mcp_elevation when
# elevation enforcement is disabled (kill switch off — the default in
# in-process TestClient tests without a full TOTP/elevation setup).
_ELEVATION_ENFORCEMENT_DISABLED: str = "elevation_enforcement_disabled"


def test_zzz_mcp_create_and_remove_group(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Create E2E_DESTROY_grp via MCP, verify name in response, then delete.

    Group manager requires an access-control feature that is not present in
    the minimal in-process TestClient server.  When create_group returns
    success=False with an error containing "not configured", the test asserts
    that exact condition and skips get/delete (nothing was created to clean up).

    Any other failure mode (auth error, validation, unexpected server error)
    still fails the test via assertion.

    When group manager IS configured (full server), the full create → verify →
    delete lifecycle executes.
    """
    create_resp = call_mcp_tool(
        test_client, "create_group", {"name": _DESTROY_GROUP}, auth_headers
    )
    _assert_resp(create_resp, label="create_group E2E_DESTROY_grp")
    payload = _mcp_result(create_resp)
    success = payload.get("success")

    if success is not True:
        # Narrow guard: only known infrastructure skip conditions are acceptable.
        # Any other failure (auth, validation, unexpected) falls through to
        # the assert below and fails the test.
        #
        # success is False  — group manager "not configured" (existing known case).
        # success is None   — payload has only an "error" key (e.g. elevation_enforcement_disabled)
        error_code = payload.get("error", "")
        if error_code == _ELEVATION_ENFORCEMENT_DISABLED:
            pytest.skip(
                "create_group requires TOTP elevation (Epic #922 / Story #925); "
                "elevation_enforcement_enabled=False in this test environment — "
                "no group was created."
            )
        assert _GROUP_MANAGER_NOT_CONFIGURED in error_code, (
            f"create_group failed for unexpected reason: {payload}"
        )
        return  # Group manager not available; nothing was created, nothing to clean up.

    assert success is True, (
        f"create_group unexpected payload (success={success!r}): {payload}"
    )
    assert payload.get("name") == _DESTROY_GROUP, (
        f"create_group name mismatch: {payload.get('name')!r}"
    )

    get_resp = call_mcp_tool(
        test_client, "get_group", {"group_name": _DESTROY_GROUP}, auth_headers
    )
    _assert_resp(get_resp, label="get_group E2E_DESTROY_grp")
    assert _mcp_result(get_resp).get("name") == _DESTROY_GROUP

    _assert_resp(
        call_mcp_tool(
            test_client, "delete_group", {"group_name": _DESTROY_GROUP}, auth_headers
        ),
        label="delete_group E2E_DESTROY_grp",
        allow_4xx=True,
    )


def test_zzz_mcp_create_and_delete_api_key(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Create an API key (description=E2E_DESTROY_key), verify key_id in list, delete."""
    create_resp = call_mcp_tool(
        test_client,
        "create_api_key",
        {"description": _DESTROY_API_KEY_DESC},
        auth_headers,
    )
    _assert_resp(create_resp, label="create_api_key E2E_DESTROY_key")
    create_payload = _mcp_result(create_resp)
    assert create_payload.get("success"), (
        f"create_api_key success=false: {create_payload}"
    )
    key_id = create_payload.get("key_id", "")
    assert key_id, f"create_api_key empty key_id: {create_payload}"

    list_resp = call_mcp_tool(test_client, "list_api_keys", {}, auth_headers)
    _assert_resp(list_resp, label="list_api_keys after create")
    listed_ids = [k.get("id", "") for k in _mcp_result(list_resp).get("keys", [])]
    assert key_id in listed_ids, f"key_id {key_id!r} not in list: {listed_ids}"

    _assert_resp(
        call_mcp_tool(test_client, "delete_api_key", {"key_id": key_id}, auth_headers),
        label="delete_api_key E2E_DESTROY_key",
        allow_4xx=True,
    )


def test_zzz_mcp_create_and_delete_mcp_credential(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Create an MCP credential (description=E2E_DESTROY_cred), verify id in list, delete."""
    create_resp = call_mcp_tool(
        test_client,
        "create_mcp_credential",
        {"description": _DESTROY_MCP_CRED_DESC},
        auth_headers,
    )
    _assert_resp(create_resp, label="create_mcp_credential E2E_DESTROY_cred")
    create_payload = _mcp_result(create_resp)
    assert create_payload.get("success"), (
        f"create_mcp_credential success=false: {create_payload}"
    )
    credential_id = create_payload.get("credential_id", "")
    assert credential_id, f"create_mcp_credential empty credential_id: {create_payload}"

    list_resp = call_mcp_tool(test_client, "list_mcp_credentials", {}, auth_headers)
    _assert_resp(list_resp, label="list_mcp_credentials after create")
    listed_ids = [
        c.get("id", "") for c in _mcp_result(list_resp).get("credentials", [])
    ]
    assert credential_id in listed_ids, (
        f"credential_id {credential_id!r} not in list: {listed_ids}"
    )

    _assert_resp(
        call_mcp_tool(
            test_client,
            "delete_mcp_credential",
            {"credential_id": credential_id},
            auth_headers,
        ),
        label="delete_mcp_credential E2E_DESTROY_cred",
        allow_4xx=True,
    )


def test_zzz_mcp_create_and_verify_user(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Create E2E_DESTROY_user via MCP and verify it appears in list_users.

    No MCP delete_user tool exists; the user is isolated in the TestClient's
    session-scoped temporary data directory and cleaned up on teardown.
    Never touches the 'admin' user.
    """
    create_resp = call_mcp_tool(
        test_client,
        "create_user",
        {
            "username": _DESTROY_USERNAME,
            "password": _DESTROY_USER_PASSWORD,
            "role": "normal_user",
        },
        auth_headers,
    )
    _assert_resp(create_resp, label="create_user E2E_DESTROY_user")
    create_payload = _mcp_result(create_resp)

    # create_user is decorated with @require_mcp_elevation (Epic #922 / Story #925).
    # When elevation enforcement is disabled (kill switch off — the default in
    # in-process TestClient tests that do not configure TOTP elevation), the
    # handler returns elevation_enforcement_disabled instead of executing.
    # Skip visibly so the test output records why the operation was not exercised.
    if create_payload.get("error") == _ELEVATION_ENFORCEMENT_DISABLED:
        pytest.skip(
            "create_user requires TOTP elevation (Epic #922); "
            "elevation_enforcement_enabled=False in this test environment — "
            "no user was created."
        )

    assert create_payload.get("success"), "create_user success=false"

    list_resp = call_mcp_tool(test_client, "list_users", {}, auth_headers)
    _assert_resp(list_resp, label="list_users after create E2E_DESTROY_user")
    listed_names = [
        u.get("username", "") for u in _mcp_result(list_resp).get("users", [])
    ]
    assert _DESTROY_USERNAME in listed_names, (
        f"{_DESTROY_USERNAME!r} not in list_users: {listed_names}"
    )


def test_zzz_mcp_check_maintenance_cycle(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Enter maintenance mode, verify in_maintenance=True, then exit.

    Story #924: The MCP ``enter_maintenance_mode`` and ``exit_maintenance_mode``
    tools were removed.  Enter/exit maintenance is now restricted to localhost-only
    REST endpoints (``POST /api/admin/maintenance/enter`` and
    ``POST /api/admin/maintenance/exit``) so that only the auto-updater (a local
    system process) can toggle maintenance, not network clients.

    FastAPI TestClient presents ``request.client.host == "testclient"`` which is
    not a valid loopback IP address, so it fails ``require_localhost`` with 403.
    The maintenance cycle therefore cannot be exercised in the Phase 3 in-process
    TestClient environment.  This scenario is covered by Phase 4 or Phase 5 tests
    where a real subprocess server listens on localhost.
    """
    pytest.skip(
        "Maintenance enter/exit removed from MCP (Story #924) and restricted to "
        "localhost-only REST endpoints; TestClient host='testclient' fails the "
        "loopback check — not testable in the Phase 3 in-process environment."
    )


def test_zzz_mcp_delete_remaining_resources(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Cleanup sweep: delete any lingering E2E_DESTROY_ group, API keys, MCP credentials.

    Group: name-based delete via delete_group (4xx = already gone, acceptable).
    API keys: list_api_keys → filter E2E_DESTROY_ description → delete by key_id.
    MCP credentials: list_mcp_credentials → filter E2E_DESTROY_ description → delete by id.
    """
    _assert_resp(
        call_mcp_tool(
            test_client, "delete_group", {"group_name": _DESTROY_GROUP}, auth_headers
        ),
        label="sweep delete_group E2E_DESTROY_grp",
        allow_4xx=True,
    )

    _sweep_by_description(
        test_client,
        auth_headers,
        list_tool="list_api_keys",
        collection_field="keys",
        id_field="id",
        delete_tool="delete_api_key",
        delete_arg="key_id",
    )

    _sweep_by_description(
        test_client,
        auth_headers,
        list_tool="list_mcp_credentials",
        collection_field="credentials",
        id_field="id",
        delete_tool="delete_mcp_credential",
        delete_arg="credential_id",
    )
