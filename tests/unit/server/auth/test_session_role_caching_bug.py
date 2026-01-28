"""
Bug #67 Regression Tests: SSO session caches role at login - role changes not reflected until re-login

This test file validates the fix for the role caching bug where:
1. User logs in with role A (e.g., normal_user)
2. Admin changes user's role to B (e.g., admin) in the database
3. User should immediately have role B permissions WITHOUT re-login

ROOT CAUSE: The _hybrid_auth_impl function checks session.role (cached in session cookie)
instead of fetching the user's CURRENT role from the database.

FIX: Fetch user from database first, then check user.has_permission() instead of session.role.

CRITICAL: These tests must FAIL before the fix is applied (TDD Red phase).
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import _hybrid_auth_impl
from code_indexer.server.web.auth import SessionData


@pytest.mark.unit
class TestSessionRoleCachingBug:
    """
    Tests for Bug #67: Session role caching prevents immediate role change reflection.

    These tests verify that:
    1. Role changes in the database are reflected immediately
    2. Admin permission checks use database role, not session role
    3. SSO users with role upgrades get immediate access
    """

    def test_hybrid_auth_uses_database_role_not_session_role(self):
        """
        Test that _hybrid_auth_impl uses database role, not cached session role.

        SCENARIO:
        - User logged in as normal_user (session cookie has role=normal_user)
        - Admin later changed user's role to admin in database
        - User should now have admin access without re-login

        EXPECTED: Admin access granted (uses database role)
        CURRENT BUG: Admin access denied (uses session role)
        """
        # Create mock request with session cookie
        mock_request = MagicMock()
        mock_request.cookies = {"session": "valid_session_cookie"}

        # Session has OLD role (normal_user) - cached at login time
        session_with_old_role = SessionData(
            username="testuser",
            role="normal_user",  # OLD role from login time
            csrf_token="csrf123",
            created_at=datetime.now(timezone.utc).timestamp(),
        )

        # Database has NEW role (admin) - changed after login
        user_with_new_role = User(
            username="testuser",
            password_hash="$2b$12$hash",
            role=UserRole.ADMIN,  # NEW role in database
            created_at=datetime.now(timezone.utc),
        )

        # Mock session manager to return session with OLD role
        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = session_with_old_role

        # Mock user manager to return user with NEW role from database
        mock_user_manager = MagicMock()
        mock_user_manager.get_user.return_value = user_with_new_role

        with patch(
            "code_indexer.server.web.auth.get_session_manager",
            return_value=mock_session_manager,
        ):
            with patch(
                "code_indexer.server.auth.dependencies.user_manager", mock_user_manager
            ):
                # Request admin access - should succeed because DATABASE role is admin
                # BUG: Currently fails because it checks SESSION role (normal_user)
                user = _hybrid_auth_impl(
                    request=mock_request,
                    credentials=None,
                    require_admin=True,
                )

                # Verify: Should return user with admin role from database
                assert user is not None
                assert user.username == "testuser"
                assert user.role == UserRole.ADMIN

                # Verify: user_manager.get_user was called to fetch current role
                mock_user_manager.get_user.assert_called_once_with("testuser")

    def test_hybrid_auth_denies_admin_when_db_role_is_not_admin(self):
        """
        Test that admin access is correctly denied when database role is not admin.

        SCENARIO:
        - User logged in as admin (session cookie has role=admin)
        - Admin later demoted user to normal_user in database
        - User should now be DENIED admin access even though session says admin

        EXPECTED: Admin access denied (database role is normal_user)
        """
        # Create mock request with session cookie
        mock_request = MagicMock()
        mock_request.cookies = {"session": "valid_session_cookie"}

        # Session has OLD role (admin) - cached at login time
        session_with_admin_role = SessionData(
            username="demoteduser",
            role="admin",  # OLD role from login time (was admin)
            csrf_token="csrf123",
            created_at=datetime.now(timezone.utc).timestamp(),
        )

        # Database has NEW role (normal_user) - demoted after login
        user_demoted_to_normal = User(
            username="demoteduser",
            password_hash="$2b$12$hash",
            role=UserRole.NORMAL_USER,  # NEW role in database (demoted)
            created_at=datetime.now(timezone.utc),
        )

        # Mock session manager to return session with admin role
        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = session_with_admin_role

        # Mock user manager to return user with normal_user role from database
        mock_user_manager = MagicMock()
        mock_user_manager.get_user.return_value = user_demoted_to_normal

        with patch(
            "code_indexer.server.web.auth.get_session_manager",
            return_value=mock_session_manager,
        ):
            with patch(
                "code_indexer.server.auth.dependencies.user_manager", mock_user_manager
            ):
                # Request admin access - should FAIL because DATABASE role is normal_user
                # Even though session says admin, database role takes precedence
                with pytest.raises(HTTPException) as exc_info:
                    _hybrid_auth_impl(
                        request=mock_request,
                        credentials=None,
                        require_admin=True,
                    )

                # Verify: Access denied with 403 or 401
                assert exc_info.value.status_code in [401, 403]

    def test_sso_user_role_upgrade_reflected_immediately(self):
        """
        Test SSO user scenario: Role upgrade from normal_user to admin is reflected immediately.

        SCENARIO (SSO-specific):
        - SSO user logs in and is assigned normal_user role based on group mapping
        - Admin later manually upgrades user to admin role in database
        - User should have admin permissions on next request without re-login

        This is the specific scenario from Bug #67 report.
        """
        mock_request = MagicMock()
        mock_request.cookies = {"session": "sso_session_cookie"}

        # SSO session created with normal_user role from group mapping
        sso_session = SessionData(
            username="sso_user@company.com",
            role="normal_user",  # Assigned from SSO group mapping at login
            csrf_token="csrf123",
            created_at=datetime.now(timezone.utc).timestamp(),
        )

        # Admin manually upgraded user to admin in database
        upgraded_sso_user = User(
            username="sso_user@company.com",
            password_hash="$2b$12$hash",
            role=UserRole.ADMIN,  # Manually upgraded in database
            created_at=datetime.now(timezone.utc),
        )

        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = sso_session

        mock_user_manager = MagicMock()
        mock_user_manager.get_user.return_value = upgraded_sso_user

        with patch(
            "code_indexer.server.web.auth.get_session_manager",
            return_value=mock_session_manager,
        ):
            with patch(
                "code_indexer.server.auth.dependencies.user_manager", mock_user_manager
            ):
                # SSO user should now have admin access
                user = _hybrid_auth_impl(
                    request=mock_request,
                    credentials=None,
                    require_admin=True,
                )

                assert user is not None
                assert user.username == "sso_user@company.com"
                assert user.role == UserRole.ADMIN
