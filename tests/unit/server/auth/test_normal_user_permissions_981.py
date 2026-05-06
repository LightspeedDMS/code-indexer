"""
Tests for Story #981: Branch-Aware Exploration for Normal Users.

AC1: Normal user has activate_repos permission.
AC2-AC6: Normal user lacks admin/power-user-only permissions.
AC7: POWER_USER permissions unchanged (regression).
AC8: ADMIN permissions unchanged (regression).
"""

from datetime import datetime, timezone

from code_indexer.server.auth.user_manager import User, UserRole


def _make_user(role: UserRole) -> User:
    return User(
        username="testuser",
        password_hash="hashed",
        role=role,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class TestNormalUserPermissions:
    """AC1/AC2-AC6: NORMAL_USER permission set for Story #981."""

    def test_normal_user_has_activate_repos(self):
        """AC1: Normal user can activate/deactivate/switch/sync their workspace."""
        user = _make_user(UserRole.NORMAL_USER)
        assert user.has_permission("activate_repos")

    def test_normal_user_has_query_repos(self):
        """Existing permission — must not regress."""
        user = _make_user(UserRole.NORMAL_USER)
        assert user.has_permission("query_repos")

    def test_normal_user_has_repository_read(self):
        """Existing permission — must not regress."""
        user = _make_user(UserRole.NORMAL_USER)
        assert user.has_permission("repository:read")

    def test_normal_user_lacks_manage_golden_repos(self):
        """AC5: Normal user cannot change golden repo branches (admin-only)."""
        user = _make_user(UserRole.NORMAL_USER)
        assert not user.has_permission("manage_golden_repos")

    def test_normal_user_lacks_manage_users(self):
        """Normal user cannot manage users."""
        user = _make_user(UserRole.NORMAL_USER)
        assert not user.has_permission("manage_users")

    def test_normal_user_lacks_repository_write(self):
        """Normal user cannot perform write file operations."""
        user = _make_user(UserRole.NORMAL_USER)
        assert not user.has_permission("repository:write")

    def test_normal_user_lacks_repository_admin(self):
        """Normal user cannot perform destructive repo operations."""
        user = _make_user(UserRole.NORMAL_USER)
        assert not user.has_permission("repository:admin")

    def test_normal_user_lacks_delegate_open(self):
        """Normal user cannot use open-ended delegation."""
        user = _make_user(UserRole.NORMAL_USER)
        assert not user.has_permission("delegate_open")


class TestPowerUserPermissionsUnchanged:
    """AC7: POWER_USER permissions must not regress after Story #981."""

    def test_power_user_has_activate_repos(self):
        user = _make_user(UserRole.POWER_USER)
        assert user.has_permission("activate_repos")

    def test_power_user_has_repository_write(self):
        user = _make_user(UserRole.POWER_USER)
        assert user.has_permission("repository:write")

    def test_power_user_has_delegate_open(self):
        user = _make_user(UserRole.POWER_USER)
        assert user.has_permission("delegate_open")

    def test_power_user_inherits_query_repos(self):
        """POWER_USER inherits NORMAL_USER permissions."""
        user = _make_user(UserRole.POWER_USER)
        assert user.has_permission("query_repos")

    def test_power_user_inherits_repository_read(self):
        user = _make_user(UserRole.POWER_USER)
        assert user.has_permission("repository:read")

    def test_power_user_lacks_manage_users(self):
        """POWER_USER still cannot manage users — admin-only."""
        user = _make_user(UserRole.POWER_USER)
        assert not user.has_permission("manage_users")

    def test_power_user_lacks_manage_golden_repos(self):
        """POWER_USER still cannot manage golden repos — admin-only."""
        user = _make_user(UserRole.POWER_USER)
        assert not user.has_permission("manage_golden_repos")


class TestSshKeyToolsAdminOnly:
    """SSH key management is server-wide; must remain restricted to admins only.

    Security fix for Story #981 code review: SSH key tool docs incorrectly had
    required_permission: activate_repos, inadvertently exposing them to normal
    users. All SSH key tools must require repository:admin (admin-only).
    """

    def test_normal_user_cannot_manage_ssh_keys(self):
        """SSH key management is server-wide; normal users must not have this permission."""
        user = _make_user(UserRole.NORMAL_USER)
        assert not user.has_permission("repository:admin")

    def test_admin_can_manage_ssh_keys(self):
        """Admin has repository:admin permission needed for SSH key management."""
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("repository:admin")


class TestAdminPermissionsUnchanged:
    """AC8: ADMIN permissions must not regress after Story #981."""

    def test_admin_has_manage_users(self):
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("manage_users")

    def test_admin_has_manage_golden_repos(self):
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("manage_golden_repos")

    def test_admin_has_activate_repos(self):
        """ADMIN inherits activate_repos via role hierarchy."""
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("activate_repos")

    def test_admin_has_repository_admin(self):
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("repository:admin")

    def test_admin_inherits_query_repos(self):
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("query_repos")

    def test_admin_inherits_repository_write(self):
        user = _make_user(UserRole.ADMIN)
        assert user.has_permission("repository:write")
