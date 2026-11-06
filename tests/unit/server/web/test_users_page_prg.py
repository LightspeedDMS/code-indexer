"""
Regression tests for the Users page PRG (Post-Redirect-Get) helpers.

After Bug v10.91.0->v10.91.2, the delete_user (and sibling) endpoints redirect
to /admin/users?success=...|error=...&u=... instead of returning the rendered
page inline. This avoids the document.write() HTMX re-init bug that left the
users table stuck on "Loading users..." after a successful delete.

These tests cover the whitelist resolver `_resolve_users_page_messages` which
maps query-string status codes to user-facing messages. Unknown codes MUST be
ignored to prevent stored/reflected XSS via the query string.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse, parse_qs

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from code_indexer.server.web.routes import (
    _USERS_PAGE_ERROR_MESSAGES,
    _USERS_PAGE_SUCCESS_MESSAGES,
    _resolve_users_page_messages,
)


def test_resolve_known_success_code_renders_message() -> None:
    success, error = _resolve_users_page_messages("user_deleted", None, "alice")
    assert success == "User 'alice' deleted successfully"
    assert error is None


def test_resolve_known_error_code_renders_message() -> None:
    success, error = _resolve_users_page_messages(None, "invalid_csrf", None)
    assert success is None
    assert error == "Invalid CSRF token"


def test_resolve_unknown_success_code_returns_none() -> None:
    # Unknown codes must NOT echo back into the page (XSS prevention).
    success, error = _resolve_users_page_messages(
        "<script>alert(1)</script>", None, "alice"
    )
    assert success is None
    assert error is None


def test_resolve_unknown_error_code_returns_none() -> None:
    success, error = _resolve_users_page_messages(
        None, "<script>alert(1)</script>", None
    )
    assert success is None
    assert error is None


def test_resolve_empty_username_does_not_crash() -> None:
    success, error = _resolve_users_page_messages("user_deleted", None, None)
    assert success == "User '' deleted successfully"
    assert error is None


def test_known_success_codes_cover_expected_actions() -> None:
    # If new actions are added, this test reminds the author to update both
    # the dict and the corresponding POST handlers.
    expected = {
        "user_deleted",
        "user_created",
        "role_updated",
        "email_updated",
        "password_changed",
    }
    assert set(_USERS_PAGE_SUCCESS_MESSAGES.keys()) == expected


def test_known_error_codes_cover_expected_failures() -> None:
    expected = {
        "invalid_csrf",
        "cannot_delete_self",
        "user_manager_unavailable",
        "passwords_mismatch",
        "invalid_role",
        "cannot_demote_self",
        "sso_password_change_denied",
        "user_not_found",
        "operation_failed",
        "user_already_exists",
    }
    assert set(_USERS_PAGE_ERROR_MESSAGES.keys()) == expected


@pytest.mark.parametrize(
    "code",
    [
        "passwords_mismatch",
        "invalid_role",
        "cannot_demote_self",
        "sso_password_change_denied",
        "user_not_found",
        "operation_failed",
    ],
)
def test_resolve_new_error_codes_render_messages(code: str) -> None:
    success, error = _resolve_users_page_messages(None, code, None)
    assert success is None
    assert error is not None and len(error) > 0


# --- The error banner shows whitelisted text only (no reflected detail) ---


def test_resolve_user_already_exists() -> None:
    _, error = _resolve_users_page_messages(None, "user_already_exists", None)
    assert error is not None
    assert "already exists" in error


def test_resolve_operation_failed_is_fixed_text() -> None:
    _, error = _resolve_users_page_messages(None, "operation_failed", None)
    assert error == "Operation failed. Check input and try again."


def test_users_page_never_reflects_a_detail_query_param() -> None:
    """A crafted link's free text never reaches the admin error banner."""
    from code_indexer.server.web import routes

    rendered: dict = {}

    def capture(request, session, *, success_message, error_message):
        rendered["error"] = error_message
        return "page"

    with (
        patch.object(routes, "_require_admin_session", return_value=MagicMock()),
        patch.object(routes, "_create_users_page_response", side_effect=capture),
    ):
        app = FastAPI()
        app.include_router(routes.web_router)
        TestClient(app).get(
            "/users",
            params={"error": "operation_failed", "detail": "Call 555-0100 now"},
        )
    assert rendered["error"] == "Operation failed. Check input and try again."


# --- Route handler exception-to-redirect tests (MR !81) ---
#
# Verify POST handlers redirect to correct PRG error codes when
# UserManager raises or returns an error.


@contextmanager
def _patched_web_client():
    """Yield (client, mock_user_manager) with session/CSRF/elevation bypassed."""
    from code_indexer.server.web import routes
    from code_indexer.server.auth import dependencies
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(routes.web_router)

    mock_admin = MagicMock()
    mock_admin.username = "admin"
    mock_admin.has_permission.return_value = True
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: mock_admin

    session = MagicMock()
    session.username = "admin"
    session.role = "admin"

    mock_um = MagicMock()
    original = dependencies.user_manager
    dependencies.user_manager = mock_um
    try:
        with (
            patch.object(routes, "_require_admin_session", return_value=session),
            patch.object(routes, "validate_login_csrf_token", return_value=True),
        ):
            yield TestClient(app, follow_redirects=False), mock_um
    finally:
        dependencies.user_manager = original


def _redirect_params(response) -> dict[str, str]:
    """Parse the 303 Location header into a flat query-param dict."""
    qs = parse_qs(urlparse(response.headers.get("location", "")).query)
    return {k: v[0] for k, v in qs.items()}


_CREATE_USER_DATA = {
    "new_username": "bob",
    "new_password": "Str0ngP@ss!",
    "confirm_password": "Str0ngP@ss!",
    "role": "normal_user",
    "csrf_token": "tok",
}


def test_create_user_valueerror_already_exists_redirects() -> None:
    """create_user: ValueError containing 'already exists' -> user_already_exists."""
    with _patched_web_client() as (client, mock_um):
        mock_um.create_user_audited.side_effect = ValueError("User already exists")
        params = _redirect_params(client.post("/users/create", data=_CREATE_USER_DATA))
    assert params["error"] == "user_already_exists"


def test_create_user_valueerror_weak_password_redirects() -> None:
    """create_user: other ValueError -> operation_failed, no detail."""
    with _patched_web_client() as (client, mock_um):
        mock_um.create_user_audited.side_effect = ValueError(
            "Password must contain a digit"
        )
        params = _redirect_params(client.post("/users/create", data=_CREATE_USER_DATA))
    assert params["error"] == "operation_failed"
    assert "detail" not in params


def test_update_user_role_false_return_redirects() -> None:
    """update_user_role: False return (user not found) -> user_not_found, like
    the password and email doors (TestUnknownTargets in test_parity_accounts)."""
    with _patched_web_client() as (client, mock_um):
        mock_um.update_user_role_audited.return_value = False
        params = _redirect_params(
            client.post(
                "/users/bob/role", data={"role": "normal_user", "csrf_token": "tok"}
            )
        )
    assert params["error"] == "user_not_found"
    assert "success" not in params


def test_update_user_role_unexpected_exception_propagates() -> None:
    """update_user_role: non-ValueError propagates (not swallowed into a redirect)."""
    with _patched_web_client() as (client, mock_um):
        mock_um.update_user_role_audited.side_effect = RuntimeError(
            "DB connection lost"
        )
        with pytest.raises(RuntimeError, match="DB connection lost"):
            client.post(
                "/users/bob/role", data={"role": "normal_user", "csrf_token": "tok"}
            )


def test_change_password_valueerror_redirects() -> None:
    """change_user_password: ValueError -> operation_failed, no detail."""
    with _patched_web_client() as (client, mock_um):
        mock_um.change_password_audited.side_effect = ValueError("Password too common")
        params = _redirect_params(
            client.post(
                "/users/bob/password",
                data={
                    "new_password": "weak",
                    "confirm_password": "weak",
                    "csrf_token": "tok",
                },
            )
        )
    assert params["error"] == "operation_failed"
    assert "detail" not in params


def test_update_email_valueerror_redirects() -> None:
    """update_user_email: ValueError -> operation_failed, no detail."""
    with _patched_web_client() as (client, mock_um):
        mock_um.update_user_email_audited.side_effect = ValueError(
            "Invalid email format"
        )
        params = _redirect_params(
            client.post(
                "/users/bob/email", data={"new_email": "bad", "csrf_token": "tok"}
            )
        )
    assert params["error"] == "operation_failed"
    assert "detail" not in params
