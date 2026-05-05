"""
Regression tests for list_global_repos handler bugs.

Bug 1: handle_list_global_repos returned 1 of 8 repos for admin users because:
  - No admin role bypass (only group-based is_admin_user was used)
  - Non-admin users would also fail if field-name mismatch between handler
    and AccessFilteringService (repo_name vs alias_name).

These tests verify:
  - Admin user (user.role == ADMIN) bypasses access filter and sees ALL repos
  - Non-admin user with no group sees only repos from filter_repo_listing result
  - Non-admin admin-group user also bypasses (via group check in access service)
  - field_name used for filtering is repo_name (not alias_name), consistent with
    _append_global_repos_to_status reference implementation
"""
import json
from datetime import datetime
from unittest.mock import Mock, patch

from code_indexer.server.mcp.handlers.repos import handle_list_global_repos
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=role,
        created_at=datetime(2024, 1, 1),
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos: set = None,  # type: ignore[assignment]
) -> Mock:
    svc = Mock()
    svc.is_admin_user = Mock(return_value=is_admin)
    svc.filter_repo_listing = Mock(
        side_effect=lambda repos, user_id: repos if is_admin else [
            r for r in repos if accessible_repos and r in accessible_repos
        ]
    )
    return svc


EIGHT_GLOBAL_REPOS = [
    {"alias_name": "cidx-meta-global", "repo_name": "cidx-meta", "repo_url": None},
    {"alias_name": "humanize-global", "repo_name": "humanize", "repo_url": None},
    {"alias_name": "shortuuid-global", "repo_name": "shortuuid", "repo_url": None},
    {"alias_name": "python-slugify-global", "repo_name": "python-slugify", "repo_url": None},
    {"alias_name": "aspnetimage-global", "repo_name": "aspnetimage", "repo_url": None},
    {"alias_name": "langfuse-api-global", "repo_name": "langfuse-api", "repo_url": None},
    {"alias_name": "langfuse-core-global", "repo_name": "langfuse-core", "repo_url": None},
    {"alias_name": "langfuse-sdk-global", "repo_name": "langfuse-sdk", "repo_url": None},
]


class TestHandleListGlobalReposAdminBypass:
    """Admin user (role=ADMIN) must see all repos regardless of group membership."""

    def test_admin_role_user_sees_all_8_repos_even_without_admin_group(self):
        """
        Bug regression: admin user with role=ADMIN must bypass access filter.

        Reproduces the reported bug: 8 repos in DB, admin user sees only 1
        because is_admin_user() (group-based) returned False.
        """
        admin_user = _make_user("Seba.Battig@lightspeeddms.com", UserRole.ADMIN)

        # Access service reports this user is NOT in the admins group
        # (the group-based check is independent of user.role)
        access_service = _make_access_service(
            is_admin=False,  # NOT in admins group
            accessible_repos={"cidx-meta"},  # Only cidx-meta accessible via group
        )

        with patch(
            "code_indexer.server.mcp.handlers.repos._get_access_filtering_service",
            return_value=access_service,
        ), patch(
            "code_indexer.server.mcp.handlers.repos._list_global_repos",
            return_value=EIGHT_GLOBAL_REPOS,
        ):
            result = handle_list_global_repos({}, admin_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        assert len(response["repos"]) == 8, (
            f"Admin user must see all 8 repos, got {len(response['repos'])}"
        )

    def test_admin_role_user_bypasses_filter_regardless_of_group(self):
        """
        Admin role (user.role == ADMIN) bypasses filtering,
        not just group-based is_admin_user check.
        """
        admin_user = _make_user("admin", UserRole.ADMIN)

        # Simulate group-based admin check returning False
        access_service = _make_access_service(is_admin=False, accessible_repos=set())

        with patch(
            "code_indexer.server.mcp.handlers.repos._get_access_filtering_service",
            return_value=access_service,
        ), patch(
            "code_indexer.server.mcp.handlers.repos._list_global_repos",
            return_value=EIGHT_GLOBAL_REPOS,
        ):
            result = handle_list_global_repos({}, admin_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        # filter_repo_listing must NOT have been called for admin role users
        access_service.filter_repo_listing.assert_not_called()
        assert len(response["repos"]) == 8

    def test_admin_group_user_also_bypasses_via_service(self):
        """
        User in admins group also bypasses (via service.is_admin_user returning True).
        This is the existing working path — must remain working.
        """
        # User with POWER_USER role but in admins group
        power_user_in_admin_group = _make_user("power-admin", UserRole.POWER_USER)
        access_service = _make_access_service(is_admin=True)  # In admins group

        with patch(
            "code_indexer.server.mcp.handlers.repos._get_access_filtering_service",
            return_value=access_service,
        ), patch(
            "code_indexer.server.mcp.handlers.repos._list_global_repos",
            return_value=EIGHT_GLOBAL_REPOS,
        ):
            result = handle_list_global_repos({}, power_user_in_admin_group)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        assert len(response["repos"]) == 8


class TestHandleListGlobalReposNonAdminFiltering:
    """Non-admin users see only repos returned by filter_repo_listing."""

    def test_non_admin_user_sees_only_accessible_repos(self):
        """
        Non-admin user sees only repos their group can access.
        """
        regular_user = _make_user("alice", UserRole.NORMAL_USER)
        # Only humanize and cidx-meta accessible (by repo_name, not alias_name)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "humanize"},
        )

        with patch(
            "code_indexer.server.mcp.handlers.repos._get_access_filtering_service",
            return_value=access_service,
        ), patch(
            "code_indexer.server.mcp.handlers.repos._list_global_repos",
            return_value=EIGHT_GLOBAL_REPOS,
        ):
            result = handle_list_global_repos({}, regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        # Should only see cidx-meta-global and humanize-global
        assert len(response["repos"]) == 2
        alias_names = {r["alias_name"] for r in response["repos"]}
        assert "cidx-meta-global" in alias_names
        assert "humanize-global" in alias_names

    def test_non_admin_user_with_no_group_sees_no_repos(self):
        """
        Non-admin user with no group membership sees no repos (empty accessible set).
        """
        regular_user = _make_user("nobody", UserRole.NORMAL_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),  # Empty
        )

        with patch(
            "code_indexer.server.mcp.handlers.repos._get_access_filtering_service",
            return_value=access_service,
        ), patch(
            "code_indexer.server.mcp.handlers.repos._list_global_repos",
            return_value=EIGHT_GLOBAL_REPOS,
        ):
            result = handle_list_global_repos({}, regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        assert len(response["repos"]) == 0

    def test_no_access_service_returns_all_repos(self):
        """
        When access filtering service is not configured, all repos are returned.
        """
        admin_user = _make_user("admin", UserRole.ADMIN)

        with patch(
            "code_indexer.server.mcp.handlers.repos._get_access_filtering_service",
            return_value=None,
        ), patch(
            "code_indexer.server.mcp.handlers.repos._list_global_repos",
            return_value=EIGHT_GLOBAL_REPOS,
        ):
            result = handle_list_global_repos({}, admin_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        assert len(response["repos"]) == 8
