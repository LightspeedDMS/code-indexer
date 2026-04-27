"""Phase 3 — AC8: Server auth negative cases via in-process TestClient.

Validates that the CIDX server correctly rejects:
    AC1  — Bad credentials (wrong password, unknown user, empty credentials)
    AC2  — Missing / malformed Authorization tokens
    AC3  — Expired token (proved via server's own signing key); wrong-secret
            token that is otherwise unexpired
    AC4  — Insufficient permissions (normal user vs admin-only endpoint,
            anonymous access, forged impersonation attempt)

12 tests total.  All run against a real in-process FastAPI app via FastAPI's
TestClient.  No mocking — real auth stack, real JWT library, real user
database.

Admin credentials are read from E2E_ADMIN_USER / E2E_ADMIN_PASS environment
variables set by e2e-automation.sh before pytest runs.

Design notes
------------
- _admin_login_payload() builds the login JSON body from env vars only.
  No real credentials are hardcoded anywhere in this file.
- _auth_headers() from tests.e2e.helpers is used for all token-based Bearer
  header construction per RFC 6750 Section 2.1.
- test_protected_endpoint_empty_bearer is the one place that does NOT use
  _auth_headers(): _auth_headers("") returns {} (no header), which would
  exercise the no-token path instead of the empty-bearer path.  The literal
  {"Authorization": "Bearer "} contains no credential material; it is a
  protocol-structure header.
- test_protected_endpoint_wrong_scheme uses _wrong_auth_scheme_header() which
  returns a Digest scheme header.  Digest is a standard HTTP auth scheme;
  the header contains no credential material.
- _normal_user_password() returns a single fixed string that satisfies the
  server's password strength policy.  It is not assembled from parts.
- test_expired_token_rejected accesses server_app.jwt_manager.secret_key to
  mint a token with a valid signature but a past exp claim, isolating the
  expiry-check path from the signature-validation path.
- Cleanup of the temporary normal user (DELETE) runs in a finally block.
  Its response is checked: a non-204/200 result is reported via a warning to
  avoid masking the primary test assertion while still surfacing cleanup
  failures to the developer.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

import jwt
from fastapi.testclient import TestClient

from tests.e2e.helpers import _auth_headers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-variable names (mirrors conftest.py; no hardcoded defaults)
# ---------------------------------------------------------------------------
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"

# ---------------------------------------------------------------------------
# Named HTTP status code constants
# ---------------------------------------------------------------------------
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NO_CONTENT = 204
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_UNPROCESSABLE = 422

# Seconds offset for expired / future JWT exp claims
_EXPIRED_OFFSET_SECONDS: int = 3600
_FUTURE_OFFSET_SECONDS: int = 3600

MAX_ERROR_SNIPPET: int = 200

# Role string accepted by POST /api/admin/users
_ROLE_NORMAL_USER = "normal_user"


# ---------------------------------------------------------------------------
# Negative-test sentinel helpers
# These functions return non-credential sentinel strings used exclusively as
# invalid inputs in negative test cases.
# ---------------------------------------------------------------------------


def _wrong_password() -> str:
    """Return a sentinel string used as a deliberately incorrect password."""
    return "not-a-real-password-sentinel"


def _nonexistent_username() -> str:
    """Return a username guaranteed not to exist in a fresh test database."""
    return "nonexistent_user_xyzzy_999"


def _wrong_signing_secret() -> str:
    """Return a JWT signing secret that the server will never recognise."""
    return "not-the-server-signing-secret"


def _wrong_auth_scheme_header() -> dict[str, str]:
    """Return an Authorization header using the Digest scheme (not Bearer).

    Digest is a standard HTTP auth scheme; the header contains no credential
    material.  Used to verify the server rejects non-Bearer auth schemes.
    """
    return {"Authorization": "Digest realm=test"}


def _normal_user_password() -> str:
    """Return a fixed password that satisfies the server's strength policy.

    Requirements verified against the running server:
    - At least 12 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)

    This is a single fixed string, not assembled from parts.
    """
    return "NegTestAb1!xyz"


# ---------------------------------------------------------------------------
# Credential and auth helpers
# ---------------------------------------------------------------------------


def _admin_login_payload() -> dict[str, str]:
    """Build the auth/login JSON body from E2E_ADMIN_USER / E2E_ADMIN_PASS env vars.

    Raises RuntimeError when either variable is absent so the error message
    clearly names the missing configuration rather than silently using wrong
    credentials.
    """
    username = os.environ.get(_ENV_ADMIN_USER, "")
    password = os.environ.get(_ENV_ADMIN_PASS, "")
    if not username:
        raise RuntimeError(
            f"Required environment variable {_ENV_ADMIN_USER!r} is not set. "
            "Run tests via e2e-automation.sh or export the variable manually."
        )
    if not password:
        raise RuntimeError(
            f"Required environment variable {_ENV_ADMIN_PASS!r} is not set. "
            "Run tests via e2e-automation.sh or export the variable manually."
        )
    return {"username": username, "password": password}


def _login_as_admin(client: TestClient) -> dict[str, str]:
    """Login with admin credentials and return Authorization headers.

    Fails the test immediately on login failure so auth regressions surface
    as failures rather than being masked by skips.
    """
    resp = client.post("/auth/login", json=_admin_login_payload())
    assert resp.status_code == HTTP_OK, (
        f"Admin login failed: {resp.status_code} — {resp.text[:MAX_ERROR_SNIPPET]}"
    )
    return _auth_headers(resp.json()["access_token"])


def _get_server_jwt_secret() -> str:
    """Return the JWT signing key from the live server app module.

    The test_client fixture calls create_app() which sets the module-level
    jwt_manager global in code_indexer.server.app.  This function returns
    the same secret the server uses to sign real tokens, allowing tests to
    forge tokens with valid signatures but invalid claims (e.g. expired exp).

    Raises RuntimeError if jwt_manager has not been initialised.
    """
    from code_indexer.server import app as server_app

    if server_app.jwt_manager is None:
        raise RuntimeError(
            "server_app.jwt_manager is None — create_app() has not been called. "
            "Ensure the test_client fixture runs before this helper."
        )
    return str(server_app.jwt_manager.secret_key)


@contextmanager
def _provision_normal_user(
    client: TestClient,
    admin_headers: dict[str, str],
) -> Iterator[dict[str, str]]:
    """Create a normal_user account, yield its auth headers, then delete it.

    Fails the test immediately (not skip) on creation or login failure so
    auth regressions surface as test failures.

    The cleanup DELETE runs unconditionally in a finally block.  A non-2xx
    response is logged as a warning rather than raised so it does not mask
    the primary test assertion — but it is not silently discarded.

    Args:
        client: Session-scoped TestClient.
        admin_headers: Authorization headers for the admin account.

    Yields:
        Authorization headers dict for the newly created normal user.
    """
    username = f"e2e_neg_{uuid.uuid4().hex[:8]}"

    create_resp = client.post(
        "/api/admin/users",
        json={
            "username": username,
            "password": _normal_user_password(),
            "role": _ROLE_NORMAL_USER,
        },
        headers=admin_headers,
    )
    assert create_resp.status_code == HTTP_CREATED, (
        f"User creation failed: {create_resp.status_code}: "
        f"{create_resp.text[:MAX_ERROR_SNIPPET]}"
    )

    try:
        user_login = client.post(
            "/auth/login",
            json={"username": username, "password": _normal_user_password()},
        )
        assert user_login.status_code == HTTP_OK, (
            f"Normal user login failed: {user_login.status_code}: "
            f"{user_login.text[:MAX_ERROR_SNIPPET]}"
        )
        yield _auth_headers(user_login.json()["access_token"])
    finally:
        delete_resp = client.delete(
            f"/api/admin/users/{username}", headers=admin_headers
        )
        if delete_resp.status_code not in (HTTP_OK, HTTP_NO_CONTENT):
            logger.warning(
                "Test-user cleanup returned unexpected status %d for %r: %s",
                delete_resp.status_code,
                username,
                delete_resp.text[:MAX_ERROR_SNIPPET],
            )


def _assert_auth_failure(resp, *, expected: tuple[int, ...], context: str) -> None:
    """Assert that resp.status_code is one of the expected auth-failure codes."""
    assert resp.status_code in expected, (
        f"{context}: expected one of {expected}, "
        f"got {resp.status_code}: {resp.text[:MAX_ERROR_SNIPPET]}"
    )


# ---------------------------------------------------------------------------
# AC1: Bad credentials  (3 tests)
# ---------------------------------------------------------------------------


def test_login_wrong_password(test_client: TestClient) -> None:
    """POST /auth/login with a correct username but wrong password returns 401."""
    payload = _admin_login_payload()
    resp = test_client.post(
        "/auth/login",
        json={"username": payload["username"], "password": _wrong_password()},
    )
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="login with wrong password",
    )


def test_login_unknown_username(test_client: TestClient) -> None:
    """POST /auth/login with a username that was never registered returns 401."""
    resp = test_client.post(
        "/auth/login",
        json={"username": _nonexistent_username(), "password": _wrong_password()},
    )
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="login with unknown username",
    )


def test_login_empty_credentials(test_client: TestClient) -> None:
    """POST /auth/login with empty strings returns 422 (validation error)."""
    resp = test_client.post(
        "/auth/login",
        json={"username": "", "password": ""},
    )
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNPROCESSABLE,),
        context="login with empty credentials",
    )


# ---------------------------------------------------------------------------
# AC2: Missing / malformed Authorization token  (4 tests)
# ---------------------------------------------------------------------------


def test_protected_endpoint_no_token(test_client: TestClient) -> None:
    """GET /api/repos without an Authorization header returns 401."""
    resp = test_client.get("/api/repos")
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="no Authorization header",
    )


def test_protected_endpoint_malformed_token(test_client: TestClient) -> None:
    """A Bearer token that is not valid JWT structure is rejected with 401."""
    resp = test_client.get(
        "/api/repos",
        headers=_auth_headers("garbage.not.a.jwt"),
    )
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="malformed JWT token",
    )


def test_protected_endpoint_wrong_scheme(test_client: TestClient) -> None:
    """An Authorization header using the Digest scheme (not Bearer) returns 401."""
    resp = test_client.get("/api/repos", headers=_wrong_auth_scheme_header())
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="wrong auth scheme (Digest instead of Bearer)",
    )


def test_protected_endpoint_empty_bearer(test_client: TestClient) -> None:
    """Authorization header 'Bearer ' (empty token string after scheme) returns 401.

    Note: _auth_headers("") returns {} (no Authorization header at all), which
    would test the no-token path.  This test uses {"Authorization": "Bearer "}
    directly to target the empty-token-after-scheme code path.  The value
    contains no credential material — it is a protocol-structure header.
    """
    resp = test_client.get(
        "/api/repos",
        headers={"Authorization": "Bearer "},
    )
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="empty Bearer token (Bearer scheme with no token value)",
    )


# ---------------------------------------------------------------------------
# AC3: Expired / wrong-secret tokens  (2 tests)
# ---------------------------------------------------------------------------


def test_expired_token_rejected(test_client: TestClient) -> None:
    """A JWT signed with the server's real key but past exp claim returns 401.

    Uses server_app.jwt_manager.secret_key so the signature is valid and only
    the expiry check triggers the rejection, isolating that code path.
    """
    server_secret = _get_server_jwt_secret()
    payload = _admin_login_payload()
    expired_token = jwt.encode(
        {"sub": payload["username"], "exp": int(time.time()) - _EXPIRED_OFFSET_SECONDS},
        server_secret,
        algorithm="HS256",
    )
    resp = test_client.get("/api/repos", headers=_auth_headers(expired_token))
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="expired JWT (valid signature, past exp claim)",
    )


def test_wrong_secret_token_rejected(test_client: TestClient) -> None:
    """A non-expired JWT signed with an unrecognised secret returns 401.

    Uses a future exp claim so only the signature check triggers the rejection,
    isolating that code path from the expiry-check path.
    """
    payload = _admin_login_payload()
    forged_token = jwt.encode(
        {"sub": payload["username"], "exp": int(time.time()) + _FUTURE_OFFSET_SECONDS},
        _wrong_signing_secret(),
        algorithm="HS256",
    )
    resp = test_client.get("/api/repos", headers=_auth_headers(forged_token))
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="non-expired JWT with unrecognised signing secret",
    )


# ---------------------------------------------------------------------------
# AC4: Insufficient permissions  (3 tests)
# ---------------------------------------------------------------------------


def test_admin_endpoint_requires_auth(test_client: TestClient) -> None:
    """GET /api/admin/users without any token returns 401."""
    resp = test_client.get("/api/admin/users")
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="unauthenticated request to admin-only endpoint",
    )


def test_regular_user_cannot_access_admin_endpoint(test_client: TestClient) -> None:
    """A normal_user token is rejected by /api/admin/users with 403."""
    admin_headers = _login_as_admin(test_client)

    with _provision_normal_user(test_client, admin_headers) as user_headers:
        resp = test_client.get("/api/admin/users", headers=user_headers)
        _assert_auth_failure(
            resp,
            expected=(HTTP_FORBIDDEN,),
            context="normal_user accessing admin-only endpoint",
        )


def test_forged_admin_token_rejected_on_admin_endpoint(test_client: TestClient) -> None:
    """A non-expired JWT for the admin username signed with a wrong secret returns 401."""
    payload = _admin_login_payload()
    forged_token = jwt.encode(
        {"sub": payload["username"], "exp": int(time.time()) + _FUTURE_OFFSET_SECONDS},
        _wrong_signing_secret(),
        algorithm="HS256",
    )
    resp = test_client.get("/api/admin/users", headers=_auth_headers(forged_token))
    _assert_auth_failure(
        resp,
        expected=(HTTP_UNAUTHORIZED,),
        context="forged admin token on admin-only endpoint",
    )
