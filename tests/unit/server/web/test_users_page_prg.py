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
    }
    assert set(_USERS_PAGE_ERROR_MESSAGES.keys()) == expected
