"""
Unit tests for admin change-password endpoint schema fix.

Verifies:
- Admin endpoint accepts body with ONLY new_password (no old_password required)
- Admin endpoint rejects empty new_password with 422
- Self-service endpoint still requires old_password (regression isolation)
"""

# ruff: noqa: F811
from unittest.mock import Mock

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    admin_client,  # noqa: F401 — imported for pytest fixture discovery
    user_client,  # noqa: F401
)

_POLICY_COMPLIANT_PASSWORD = "NewPass1!"


class TestAdminChangePassword:
    """PUT /api/admin/users/{username}/change-password"""

    def test_admin_change_password_without_old_password_succeeds(self, admin_client):
        """Admin endpoint accepts body with ONLY new_password — no old_password needed."""
        handler = _find_route_handler(
            "/api/admin/users/{username}/change-password", "PUT"
        )
        mock_um = Mock()
        mock_um.change_password.return_value = True

        with _patch_closure(handler, "user_manager", mock_um):
            response = admin_client.put(
                "/api/admin/users/someuser/change-password",
                json={"new_password": _POLICY_COMPLIANT_PASSWORD},
            )

        assert response.status_code == 200, response.text

    def test_admin_change_password_with_empty_new_password_rejected(self, admin_client):
        """Admin endpoint rejects empty new_password with 422 (min_length=1 violated)."""
        response = admin_client.put(
            "/api/admin/users/someuser/change-password",
            json={"new_password": ""},
        )
        assert response.status_code == 422

    def test_self_change_password_still_requires_old_password(self, user_client):
        """Self-service endpoint returns 422 when old_password is missing."""
        response = user_client.put(
            "/api/users/change-password",
            json={"new_password": _POLICY_COMPLIANT_PASSWORD},
        )
        assert response.status_code == 422
