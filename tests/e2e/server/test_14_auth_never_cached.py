"""Phase 3 — Story #1125: Auth-Bearing-Data-Never-Cached.

Proves that auth-bearing data (user existence) is NEVER served from a TTL
cache.  User deletion takes effect on the NEXT request, with no sleep required.

Design
------
The repo_config and query caches have ~30s TTL.  If auth decisions were
cached alongside query state, a deleted user would still succeed for up to
30s.  These tests assert HTTP 401 on the VERY NEXT request after deletion —
no sleep.

Credential under test: user-login JWT (POST /auth/login -> access_token).

API-key-specific revocation is NOT covered here because API keys cannot
authenticate requests as Bearer tokens on this server (that feature was
reverted and is out of scope).  There is also no separate user-disable
endpoint distinct from DELETE — only role changes are available via PUT
/api/admin/users/{username}.  The delete path IS the revocation path.

Test 1 — Deleted user 401s immediately (the invariant)
  1. Create a test user via the admin REST front door (POST /api/admin/users).
  2. Login AS that user -> JWT access_token.
  3. Activate the seeded golden repo alias for that user (power_user role
     permits activation) — poll job to completion.
  4. WARM: run a real POST /api/query with the user's JWT against the alias
     -> assert 200 (warms the query-path and repo_config caches under that
     user's auth context).
  5. DELETE the user via the admin front door
     (DELETE /api/admin/users/{username}).
  6. NEXT request with that user's JWT (re-run the query) returns 401
     IMMEDIATELY — no sleep.  Assert 401.
     Immediacy within the ~30s cache TTL proves auth is not cached.

Test 2 — Control / mutation proof
  After the delete, a DIFFERENT non-deleted credential (the admin JWT) still
  returns 200 — proving the 401 is from the user deletion, not a general
  auth break.

Credentials from env: E2E_ADMIN_USER, E2E_ADMIN_PASS (set by e2e-automation.sh).
Requires VOYAGE_API_KEY for the warm-up query (seeded_indexed_client).
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import _auth_headers, require_voyage_key
from tests.e2e.server.mcp_helpers import call_mcp_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: TestClient, username: str, password: str) -> str:
    """POST /auth/login and return the access_token. Fails loudly on error."""
    resp = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, (
        f"Login for {username!r} failed: HTTP {resp.status_code} — {resp.text[:300]}"
    )
    token = resp.json().get("access_token")
    assert token, f"Login response missing access_token: {resp.json()}"
    return str(token)


def _create_test_user(
    client: TestClient,
    admin_headers: dict,
    username: str,
    password: str,
) -> None:
    """Create a test user via the MCP create_user tool.

    Users are created as power_user so they can activate repos for querying.
    Elevation enforcement is disabled by default in the test environment, so
    the @require_mcp_elevation decorator passes through without a TOTP challenge.
    """
    resp = call_mcp_tool(
        client,
        "create_user",
        {
            "username": username,
            "password": password,
            "role": "power_user",
        },
        admin_headers,
    )
    assert resp.status_code == 200, (
        f"create_user MCP call failed: HTTP {resp.status_code} — {resp.text[:300]}"
    )
    body = resp.json()
    result = body.get("result", {})
    content = result.get("content", [{}])
    # MCP result content is a list of {"type": "text", "text": "<json>"}.
    # Parse the inner JSON text to check the tool success flag.
    assert content and isinstance(content, list), (
        f"create_user: unexpected result.content shape: {body}"
    )
    raw = content[0].get("text", "{}")
    try:
        inner = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"create_user: failed to parse inner JSON from MCP result: {raw!r}"
        ) from exc
    assert inner.get("success") is True, (
        f"create_user returned success=False for {username!r}: {body}"
    )


def _activate_repo_for_user(
    client: TestClient,
    user_token: str,
    alias: str,
) -> None:
    """Activate the golden repo for the test user so they can query it.

    power_user role is required (enforced by the activate endpoint).  The
    helper polls until the activation job completes so the warm query can
    proceed immediately without a race condition.
    """
    resp = client.post(
        "/api/repos/activate",
        json={"golden_repo_alias": alias},
        headers=_auth_headers(user_token),
    )
    assert resp.status_code in (200, 202), (
        f"activate repo failed: HTTP {resp.status_code} — {resp.text[:300]}"
    )
    job_id = resp.json().get("job_id")
    assert job_id, f"activate: missing job_id in response: {resp.json()}"

    # Poll until the activation job finishes (bounded loop — Messi Rule #14).
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        job_resp = client.get(
            f"/api/jobs/{job_id}",
            headers=_auth_headers(user_token),
        )
        if job_resp.status_code == 200:
            body = job_resp.json()
            job_status = body.get("status")
            if job_status in ("completed", "failed", "error"):
                assert job_status == "completed", (
                    f"activate job {job_id!r} ended with status {job_status!r}: {body}"
                )
                return
        time.sleep(2.0)
    raise AssertionError(f"activate job {job_id!r} did not complete within 120s")


def _warm_query_jwt(
    client: TestClient,
    user_token: str,
    alias: str,
) -> None:
    """Issue a semantic query against *alias* using *user_token* as Bearer JWT.

    Warms the query-path and repo_config caches so that if auth decisions were
    cached alongside query state, a subsequent user deletion might be masked.
    Asserts HTTP 200 to confirm the JWT is valid before deletion.
    """
    resp = client.post(
        "/api/query",
        json={
            "query_text": "template rendering",
            "repository_alias": alias,
            "max_results": 1,
        },
        headers=_auth_headers(user_token),
    )
    assert resp.status_code == 200, (
        f"Warm query failed: HTTP {resp.status_code} — {resp.text[:300]} "
        "(expected 200 to confirm JWT is valid before user deletion)"
    )


def _delete_user_via_front_door(
    client: TestClient,
    admin_headers: dict,
    username: str,
) -> None:
    """Delete *username* via the admin REST front door.

    Calls DELETE /api/admin/users/{username} with admin credentials.
    This is the canonical front-door path: the server validates the admin JWT,
    looks up the user in the DB, and deletes them — the exact same code path
    the auth middleware would use to check user existence on the next request.

    Returns 200 on success.  Asserts loudly on any other status.
    """
    resp = client.delete(
        f"/api/admin/users/{username}",
        headers=admin_headers,
    )
    assert resp.status_code == 200, (
        f"DELETE /api/admin/users/{username} failed: "
        f"HTTP {resp.status_code} — {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded_indexed_client")
class TestAuthNeverCached:
    """Story #1125: prove auth-bearing data is checked live, never from cache."""

    def test_deleted_user_401s_immediately(
        self,
        test_client: TestClient,
        auth_headers: dict,
        seeded_indexed_client: tuple[TestClient, str],
    ) -> None:
        """Deleted user JWT 401s on the very next request after deletion.

        Flow:
          create user -> login -> activate repo -> warm query (200) ->
          DELETE user -> next query with same JWT -> 401 immediately.

        No sleep between deletion and the assertion.  A 200 here would mean
        the server returned a cached auth decision for a deleted user.
        """
        require_voyage_key()

        client, alias = seeded_indexed_client
        username = f"del_user_{uuid.uuid4().hex[:8]}"
        password = uuid.uuid4().hex + "Aa1!"

        try:
            # Step 1: create test user via admin MCP tool.
            _create_test_user(client, auth_headers, username, password)

            # Step 2: login as the new user to obtain a JWT.
            user_token = _login(client, username, password)

            # Step 3: activate the seeded golden repo so the user can query it.
            # power_user role was requested on create, so activation is permitted.
            _activate_repo_for_user(client, user_token, alias)

            # Step 4: warm query — must succeed (proves JWT is valid).
            # This warms the query-path caches under this user's auth context.
            _warm_query_jwt(client, user_token, alias)

            # Step 5: delete the user via the admin REST front door.
            _delete_user_via_front_door(client, auth_headers, username)

            # Step 6: next request with the same (now-deleted user's) JWT must
            # return 401 IMMEDIATELY — no sleep.
            # A 200 would prove the server served a cached auth decision.
            resp = client.post(
                "/api/query",
                json={
                    "query_text": "template rendering",
                    "repository_alias": alias,
                    "max_results": 1,
                },
                headers=_auth_headers(user_token),
            )
            assert resp.status_code == 401, (
                f"FAILED: expected 401 after user deletion, "
                f"got HTTP {resp.status_code} — {resp.text[:300]}. "
                "This means the server served a CACHED auth decision for a deleted user."
            )

        finally:
            # Best-effort cleanup: the DELETE may already have succeeded above.
            # Attempt to delete again; ignore 404 (already gone) and any error.
            try:
                resp = client.delete(
                    f"/api/admin/users/{username}",
                    headers=auth_headers,
                )
                if resp.status_code not in (200, 404):
                    logger.warning(
                        "cleanup: unexpected status %d deleting user %r: %s",
                        resp.status_code,
                        username,
                        resp.text[:200],
                    )
            except Exception as exc:
                logger.warning("cleanup: failed to delete user %r: %s", username, exc)

    def test_control_admin_credential_still_works_after_delete(
        self,
        test_client: TestClient,
        admin_token: str,
        seeded_indexed_client: tuple[TestClient, str],
    ) -> None:
        """Admin JWT works after test_deleted_user_401s_immediately runs.

        Proves the 401 in the delete test came from user deletion, not an
        unrelated auth stack break.  If this test fails while the delete test
        passes, there is a regression in admin auth (not in cache invalidation).
        """
        require_voyage_key()

        client, alias = seeded_indexed_client

        resp = client.post(
            "/api/query",
            json={
                "query_text": "template rendering",
                "repository_alias": alias,
                "max_results": 1,
            },
            headers=_auth_headers(admin_token),
        )
        assert resp.status_code == 200, (
            f"CONTROL FAILED: admin JWT returned HTTP {resp.status_code} — "
            f"{resp.text[:300]}. "
            "Expected 200. This is an unrelated auth regression, not a cache-invalidation bug."
        )
