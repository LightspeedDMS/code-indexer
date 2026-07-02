"""Tests for Story #992: SSH and group CRUD consolidation.

10 narrow tools consolidated into 4 action-param tools:
  - manage_ssh_key (replaces cidx_ssh_key_create/delete/assign_host/show_public)
  - list_ssh_keys (replaces cidx_ssh_key_list, renamed)
  - manage_group_members (replaces add_member_to_group/remove_member_from_group)
  - manage_group_repos (replaces add_repos_to_group/remove_repo_from_group/bulk_remove_repos_from_group)

Hard-cut: old tool names absent from all registries.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.mcp.tools import TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Elevation helpers (mirrors pattern from test_group_mcp_tools.py)
# ---------------------------------------------------------------------------

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_TEST_SESSION_KEY = "test-elevation-session-key-992"


@contextlib.contextmanager
def _with_elevation(username: str, tmp_path_str: str):
    """Provide a real active elevation window for tests."""
    esm = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(Path(tmp_path_str) / "elev992.db"),
    )
    esm.create(
        session_key=_TEST_SESSION_KEY,
        username=username,
        elevated_from_ip=None,
        scope="full",
    )
    totp_svc = MagicMock()
    totp_svc.is_mfa_enabled.return_value = True
    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, esm),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        yield _TEST_SESSION_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    return User(
        username="admin_992",
        password_hash="$2b$12$hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def temp_groups_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_groups_db):
    from code_indexer.server.services.group_access_manager import GroupAccessManager

    return GroupAccessManager(temp_groups_db)


# =============================================================================
# AC1: New tools present in HANDLER_REGISTRY
# =============================================================================


class TestNewToolsInHandlerRegistry:
    """4 new unified tools must be registered."""

    def test_manage_ssh_key_registered(self):
        assert "manage_ssh_key" in HANDLER_REGISTRY

    def test_list_ssh_keys_registered(self):
        assert "list_ssh_keys" in HANDLER_REGISTRY

    def test_manage_group_members_registered(self):
        assert "manage_group_members" in HANDLER_REGISTRY

    def test_manage_group_repos_registered(self):
        assert "manage_group_repos" in HANDLER_REGISTRY


# =============================================================================
# AC2: Old narrow tools ABSENT from HANDLER_REGISTRY (hard-cut)
# =============================================================================


class TestOldToolsAbsentFromHandlerRegistry:
    """10 old narrow tools must NOT be registered after hard-cut."""

    def test_cidx_ssh_key_create_removed(self):
        assert "cidx_ssh_key_create" not in HANDLER_REGISTRY

    def test_cidx_ssh_key_delete_removed(self):
        assert "cidx_ssh_key_delete" not in HANDLER_REGISTRY

    def test_cidx_ssh_key_list_removed(self):
        assert "cidx_ssh_key_list" not in HANDLER_REGISTRY

    def test_cidx_ssh_key_show_public_removed(self):
        assert "cidx_ssh_key_show_public" not in HANDLER_REGISTRY

    def test_cidx_ssh_key_assign_host_removed(self):
        assert "cidx_ssh_key_assign_host" not in HANDLER_REGISTRY

    def test_add_member_to_group_removed(self):
        assert "add_member_to_group" not in HANDLER_REGISTRY

    def test_remove_member_from_group_removed(self):
        assert "remove_member_from_group" not in HANDLER_REGISTRY

    def test_add_repos_to_group_removed(self):
        assert "add_repos_to_group" not in HANDLER_REGISTRY

    def test_remove_repo_from_group_removed(self):
        assert "remove_repo_from_group" not in HANDLER_REGISTRY

    def test_bulk_remove_repos_from_group_removed(self):
        assert "bulk_remove_repos_from_group" not in HANDLER_REGISTRY


# =============================================================================
# AC3: New tools present in TOOL_REGISTRY (tool docs loaded)
# =============================================================================


class TestNewToolsInToolRegistry:
    """4 new unified tools must have tool docs loaded."""

    def test_manage_ssh_key_in_tool_registry(self):
        assert "manage_ssh_key" in TOOL_REGISTRY

    def test_list_ssh_keys_in_tool_registry(self):
        assert "list_ssh_keys" in TOOL_REGISTRY

    def test_manage_group_members_in_tool_registry(self):
        assert "manage_group_members" in TOOL_REGISTRY

    def test_manage_group_repos_in_tool_registry(self):
        assert "manage_group_repos" in TOOL_REGISTRY


# =============================================================================
# AC4: Old tool docs ABSENT from TOOL_REGISTRY (deleted)
# =============================================================================


class TestOldToolsAbsentFromToolRegistry:
    """Old tool names must not appear in TOOL_REGISTRY after doc deletion."""

    def test_cidx_ssh_key_create_doc_gone(self):
        assert "cidx_ssh_key_create" not in TOOL_REGISTRY

    def test_cidx_ssh_key_delete_doc_gone(self):
        assert "cidx_ssh_key_delete" not in TOOL_REGISTRY

    def test_cidx_ssh_key_list_doc_gone(self):
        assert "cidx_ssh_key_list" not in TOOL_REGISTRY

    def test_cidx_ssh_key_show_public_doc_gone(self):
        assert "cidx_ssh_key_show_public" not in TOOL_REGISTRY

    def test_cidx_ssh_key_assign_host_doc_gone(self):
        assert "cidx_ssh_key_assign_host" not in TOOL_REGISTRY

    def test_add_member_to_group_doc_gone(self):
        assert "add_member_to_group" not in TOOL_REGISTRY

    def test_remove_member_from_group_doc_gone(self):
        assert "remove_member_from_group" not in TOOL_REGISTRY

    def test_add_repos_to_group_doc_gone(self):
        assert "add_repos_to_group" not in TOOL_REGISTRY

    def test_remove_repo_from_group_doc_gone(self):
        assert "remove_repo_from_group" not in TOOL_REGISTRY

    def test_bulk_remove_repos_from_group_doc_gone(self):
        assert "bulk_remove_repos_from_group" not in TOOL_REGISTRY


# =============================================================================
# AC5: Tool doc schemas for new tools
# =============================================================================


class TestManageSshKeyToolDoc:
    """manage_ssh_key tool doc has correct structure."""

    def test_has_action_parameter(self):
        schema = TOOL_REGISTRY["manage_ssh_key"]["inputSchema"]
        assert "action" in schema["properties"]

    def test_action_is_required(self):
        schema = TOOL_REGISTRY["manage_ssh_key"]["inputSchema"]
        assert "action" in schema.get("required", [])

    def test_has_correct_permission(self):
        assert (
            TOOL_REGISTRY["manage_ssh_key"]["required_permission"] == "repository:admin"
        )

    def test_has_description(self):
        tool = TOOL_REGISTRY["manage_ssh_key"]
        assert "description" in tool
        assert len(tool["description"]) > 10


class TestListSshKeysToolDoc:
    """list_ssh_keys tool doc has correct structure."""

    def test_has_no_required_params(self):
        schema = TOOL_REGISTRY["list_ssh_keys"]["inputSchema"]
        assert schema.get("required", []) == []

    def test_has_correct_permission(self):
        assert (
            TOOL_REGISTRY["list_ssh_keys"]["required_permission"] == "repository:admin"
        )


class TestManageGroupMembersToolDoc:
    """manage_group_members tool doc has correct structure."""

    def test_has_action_parameter(self):
        schema = TOOL_REGISTRY["manage_group_members"]["inputSchema"]
        assert "action" in schema["properties"]

    def test_action_is_required(self):
        schema = TOOL_REGISTRY["manage_group_members"]["inputSchema"]
        assert "action" in schema.get("required", [])

    def test_has_group_id_parameter(self):
        schema = TOOL_REGISTRY["manage_group_members"]["inputSchema"]
        assert "group_id" in schema["properties"]

    def test_has_correct_permission(self):
        assert (
            TOOL_REGISTRY["manage_group_members"]["required_permission"]
            == "manage_users"
        )


class TestManageGroupReposToolDoc:
    """manage_group_repos tool doc has correct structure."""

    def test_has_action_parameter(self):
        schema = TOOL_REGISTRY["manage_group_repos"]["inputSchema"]
        assert "action" in schema["properties"]

    def test_action_is_required(self):
        schema = TOOL_REGISTRY["manage_group_repos"]["inputSchema"]
        assert "action" in schema.get("required", [])

    def test_has_group_id_parameter(self):
        schema = TOOL_REGISTRY["manage_group_repos"]["inputSchema"]
        assert "group_id" in schema["properties"]

    def test_has_correct_permission(self):
        assert (
            TOOL_REGISTRY["manage_group_repos"]["required_permission"] == "manage_users"
        )


# =============================================================================
# AC6: SSH dispatcher action routing (handlers functional)
# =============================================================================


class TestManageSshKeyDispatcher:
    """manage_ssh_key dispatcher routes to correct inner handler by action."""

    def _call(self, args, user):
        handler = HANDLER_REGISTRY["manage_ssh_key"]
        result = handler(args, user)
        return json.loads(result["content"][0]["text"])

    def test_invalid_action_returns_error(self, admin_user):
        content = self._call({"action": "bogus_action"}, admin_user)
        assert content["success"] is False
        assert (
            "action" in content["error"].lower()
            or "invalid" in content["error"].lower()
        )

    def test_missing_action_returns_error(self, admin_user):
        content = self._call({}, admin_user)
        assert content["success"] is False

    def test_create_action_requires_name(self, admin_user):
        """create action with no name returns error about missing name."""
        with patch("code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager"):
            content = self._call({"action": "create"}, admin_user)
        assert content["success"] is False
        assert "name" in content["error"].lower()

    def test_delete_action_requires_name(self, admin_user):
        """delete action with no name returns error about missing name."""
        with patch("code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager"):
            content = self._call({"action": "delete"}, admin_user)
        assert content["success"] is False
        assert "name" in content["error"].lower()

    def test_show_public_action_requires_name(self, admin_user):
        """show_public action with no name returns error about missing name."""
        with patch("code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager"):
            content = self._call({"action": "show_public"}, admin_user)
        assert content["success"] is False
        assert "name" in content["error"].lower()

    def test_assign_host_action_requires_name_and_hostname(self, admin_user):
        """assign_host action with no name returns error about missing name."""
        with patch("code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager"):
            content = self._call({"action": "assign_host"}, admin_user)
        assert content["success"] is False

    def test_create_action_delegates_to_inner_handler(self, admin_user):
        """create action path calls through to manager.create_key."""
        mock_manager = MagicMock()
        mock_meta = MagicMock()
        mock_meta.name = "test-key"
        mock_meta.fingerprint = "SHA256:abc123"
        mock_meta.key_type = "ed25519"
        mock_meta.email = None
        mock_meta.description = None
        mock_meta.public_key = "ssh-ed25519 AAAA..."
        mock_manager.create_key.return_value = mock_meta

        with patch(
            "code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager",
            return_value=mock_manager,
        ):
            content = self._call(
                {"action": "create", "name": "test-key"},
                admin_user,
            )
        assert content["success"] is True
        mock_manager.create_key.assert_called_once()


# =============================================================================
# AC7: list_ssh_keys functional test
# =============================================================================


class TestListSshKeysHandler:
    """list_ssh_keys handler delegates to list_keys."""

    def test_list_ssh_keys_returns_success(self, admin_user):
        mock_manager = MagicMock()
        mock_result = MagicMock()
        mock_result.managed = []
        mock_result.unmanaged = []
        mock_manager.list_keys.return_value = mock_result

        with patch(
            "code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager",
            return_value=mock_manager,
        ):
            handler = HANDLER_REGISTRY["list_ssh_keys"]
            result = handler({}, admin_user)
            content = json.loads(result["content"][0]["text"])
        assert content["success"] is True
        assert "managed" in content
        assert "unmanaged" in content


# =============================================================================
# AC8: manage_group_members dispatcher routing
# =============================================================================


class TestManageGroupMembersDispatcher:
    """manage_group_members dispatcher routes add/remove by action."""

    def _call(self, args, user, **kwargs):
        handler = HANDLER_REGISTRY["manage_group_members"]
        result = handler(args, user, **kwargs)
        return json.loads(result["content"][0]["text"])

    def test_invalid_action_returns_error(self, admin_user):
        content = self._call({"action": "bogus"}, admin_user)
        assert content["success"] is False
        assert (
            "action" in content["error"].lower()
            or "invalid" in content["error"].lower()
        )

    def test_missing_action_returns_error(self, admin_user):
        content = self._call({}, admin_user)
        assert content["success"] is False

    def test_add_action_routes_through_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """add action reaches inner handler which requires elevation."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            content = self._call(
                {"action": "add", "group_id": str(group_id), "user_id": "test_user"},
                admin_user,
                session_key=session_key,
            )
        assert content["success"] is True

    def test_remove_action_routes_through_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """remove action reaches inner handler which requires elevation."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            group_manager.assign_user_to_group("del_user", group_id, "admin_992")
            content = self._call(
                {"action": "remove", "group_id": str(group_id), "user_id": "del_user"},
                admin_user,
                session_key=session_key,
            )
        assert content["success"] is True

    def test_add_action_blocked_without_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """add action inner handler blocks when no elevation window."""
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        esm = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "no_window.db"),
        )
        totp_svc = MagicMock()
        totp_svc.is_mfa_enabled.return_value = True
        with (
            patch(_ENFORCEMENT_PATH, return_value=True),
            patch(_ESM_PATH, esm),
            patch(_TOTP_PATH, return_value=totp_svc),
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            content = self._call(
                {
                    "action": "add",
                    "group_id": str(group_id),
                    "user_id": "blocked_user",
                },
                admin_user,
                session_key="no-window-key",
            )
        assert content.get("error") == "elevation_required"


# =============================================================================
# AC9: manage_group_repos dispatcher routing
# =============================================================================


class TestManageGroupReposDispatcher:
    """manage_group_repos dispatcher routes add/remove/bulk_remove by action."""

    def _call(self, args, user, **kwargs):
        handler = HANDLER_REGISTRY["manage_group_repos"]
        result = handler(args, user, **kwargs)
        return json.loads(result["content"][0]["text"])

    def test_invalid_action_returns_error(self, admin_user):
        content = self._call({"action": "noop"}, admin_user)
        assert content["success"] is False

    def test_missing_action_returns_error(self, admin_user):
        content = self._call({}, admin_user)
        assert content["success"] is False

    def test_add_action_routes_through_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """add action grants repo access when elevated."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            content = self._call(
                {
                    "action": "add",
                    "group_id": str(group_id),
                    "repos": ["svc-alpha"],
                },
                admin_user,
                session_key=session_key,
            )
        assert content["success"] is True
        assert "added_count" in content

    def test_remove_action_routes_through_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """remove action revokes repo access when elevated."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            group_manager.grant_repo_access("svc-beta", group_id, "admin_992")
            content = self._call(
                {
                    "action": "remove",
                    "group_id": str(group_id),
                    "repo_name": "svc-beta",
                },
                admin_user,
                session_key=session_key,
            )
        assert content["success"] is True

    def test_bulk_remove_action_routes_through_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """bulk_remove action removes multiple repos when elevated."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            group_manager.grant_repo_access("bulk-1", group_id, "admin_992")
            group_manager.grant_repo_access("bulk-2", group_id, "admin_992")
            content = self._call(
                {
                    "action": "bulk_remove",
                    "group_id": str(group_id),
                    "repos": ["bulk-1", "bulk-2"],
                },
                admin_user,
                session_key=session_key,
            )
        assert content["success"] is True
        assert "removed_count" in content

    def test_add_action_blocked_without_elevation(
        self, admin_user, group_manager, tmp_path
    ):
        """add action inner handler blocks when no elevation window."""
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        esm = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "no_window_repos.db"),
        )
        totp_svc = MagicMock()
        totp_svc.is_mfa_enabled.return_value = True
        with (
            patch(_ENFORCEMENT_PATH, return_value=True),
            patch(_ESM_PATH, esm),
            patch(_TOTP_PATH, return_value=totp_svc),
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=group_manager,
            ),
        ):
            groups = group_manager.get_all_groups()
            group_id = groups[0].id
            content = self._call(
                {
                    "action": "add",
                    "group_id": str(group_id),
                    "repos": ["blocked-repo"],
                },
                admin_user,
                session_key="no-window-key",
            )
        assert content.get("error") == "elevation_required"


# =============================================================================
# AC10: Elevation preserved — inner handlers have __wrapped__ attribute
# =============================================================================


class TestElevationPreservedOnInnerHandlers:
    """Inner group handlers must still have @require_mcp_elevation() applied."""

    @pytest.fixture
    def admin_mod(self, tmp_path):
        with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmp_path)}):
            import code_indexer.server.mcp.handlers.admin as mod

            return mod

    def test_add_member_inner_has_wrapped(self, admin_mod):
        handler = getattr(admin_mod, "_add_member", None)
        assert handler is not None, "_add_member not found in admin module"
        assert hasattr(handler, "__wrapped__"), (
            "_add_member must have __wrapped__ (elevation applied)"
        )

    def test_remove_member_inner_has_wrapped(self, admin_mod):
        handler = getattr(admin_mod, "_remove_member", None)
        assert handler is not None, "_remove_member not found in admin module"
        assert hasattr(handler, "__wrapped__"), (
            "_remove_member must have __wrapped__ (elevation applied)"
        )

    def test_add_repos_inner_has_wrapped(self, admin_mod):
        handler = getattr(admin_mod, "_add_repos", None)
        assert handler is not None, "_add_repos not found in admin module"
        assert hasattr(handler, "__wrapped__"), (
            "_add_repos must have __wrapped__ (elevation applied)"
        )

    def test_remove_repo_inner_has_wrapped(self, admin_mod):
        handler = getattr(admin_mod, "_remove_repo", None)
        assert handler is not None, "_remove_repo not found in admin module"
        assert hasattr(handler, "__wrapped__"), (
            "_remove_repo must have __wrapped__ (elevation applied)"
        )

    def test_bulk_remove_repos_inner_has_wrapped(self, admin_mod):
        handler = getattr(admin_mod, "_bulk_remove_repos", None)
        assert handler is not None, "_bulk_remove_repos not found in admin module"
        assert hasattr(handler, "__wrapped__"), (
            "_bulk_remove_repos must have __wrapped__ (elevation applied)"
        )
