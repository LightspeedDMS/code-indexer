"""
Tests for Credential MCP Handlers - Manager Access Pattern Verification.

Story #743 Bug Fix: The handlers previously accessed app_module.api_key_manager
and app_module.mcp_credential_manager, but these managers did NOT exist as
module-level variables in app.py.

After the fix:
- api_key_manager is instantiated on-demand using ApiKeyManager(user_manager=...)
- mcp_credential_manager is accessed via dependencies.mcp_credential_manager

These tests verify the correct access patterns are used.
"""

import pytest
import json
from datetime import datetime, timezone

from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole

_ATTR_ERR_MSG = "has no attribute 'mcp_credential_manager'"


def _assert_no_mcp_credential_manager_attribute_error(result: dict) -> None:
    """Assert mcp_credential_manager AttributeError is absent from any response shape.

    Accepts both MCP-wrapped content dicts and raw elevation-gate error dicts
    (introduced in Story #925).  In either case the invariant is that the
    mcp_credential_manager AttributeError message must not appear.
    """
    if "content" in result:
        content = json.loads(result["content"][0]["text"])
        if "error" in content:
            assert _ATTR_ERR_MSG not in content["error"], (
                f"MCP response must not contain AttributeError: {content['error']}"
            )
    else:
        assert "error" in result, f"Unexpected response format: {result}"
        assert _ATTR_ERR_MSG not in result.get("error", ""), (
            f"Raw error response must not contain AttributeError: {result['error']}"
        )
        assert _ATTR_ERR_MSG not in result.get("message", ""), (
            f"Raw error message must not contain AttributeError: {result.get('message')}"
        )


@pytest.fixture
def normal_user():
    """Create a normal user for testing."""
    return User(
        username="normal_test",
        password_hash="$2b$12$hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


class TestManagerAccessPatterns:
    """
    Tests that verify the correct manager access patterns are used.

    These tests document and verify that the handlers access managers
    through the correct patterns after the bug fix.
    """

    def test_dependencies_has_mcp_credential_manager(self):
        """
        Verify that mcp_credential_manager IS available in dependencies module.

        This confirms the correct access pattern: dependencies.mcp_credential_manager
        """
        from code_indexer.server.auth import dependencies

        assert hasattr(dependencies, "mcp_credential_manager"), (
            "mcp_credential_manager should be available in dependencies module"
        )

    def test_api_key_manager_not_in_app_module(self):
        """
        Verify that api_key_manager is NOT a module-level attribute in app.py.

        The correct pattern is to instantiate ApiKeyManager on-demand.
        """
        from code_indexer.server import app as app_module

        assert not hasattr(app_module, "api_key_manager"), (
            "api_key_manager should NOT be a module-level attribute in app.py"
        )

    def test_mcp_credential_manager_not_in_app_module(self):
        """
        Verify that mcp_credential_manager is NOT a module-level attribute in app.py.

        The correct pattern is to access via dependencies.mcp_credential_manager.
        """
        from code_indexer.server import app as app_module

        assert not hasattr(app_module, "mcp_credential_manager"), (
            "mcp_credential_manager should NOT be a module-level attribute in app.py"
        )

    def test_user_manager_is_in_app_module(self):
        """
        Verify that user_manager DOES exist in app_module.

        This shows the correct export pattern for globally-available managers.
        """
        from code_indexer.server import app as app_module

        assert hasattr(app_module, "user_manager"), (
            "user_manager should exist as a module-level attribute in app.py"
        )


class TestCredentialHandlersNoAttributeError:
    """
    Tests that verify credential handlers do NOT raise AttributeError.

    After the bug fix, the handlers correctly access managers without
    triggering AttributeError. These tests run WITHOUT mocking app_module.

    Note: The handlers may still return error responses if the managers
    are not fully initialized (e.g., in test environment), but they should
    NOT raise AttributeError for missing module attributes.
    """

    def test_create_api_key_handler_no_attribute_error(self, normal_user):
        """
        Verify create_api_key handler does NOT raise AttributeError.

        After the fix, ApiKeyManager is instantiated on-demand, so we won't
        get "module has no attribute 'api_key_manager'".
        """
        handler = HANDLER_REGISTRY["create_api_key"]

        # Should NOT raise AttributeError - the handler now uses the correct pattern
        # It may return an error response if user_manager is not fully initialized,
        # but it should not raise AttributeError for missing module attribute
        try:
            result = handler({"description": "Test key"}, normal_user)
            # If we get here, no AttributeError - check for proper MCP response format
            # or raw elevation-gate error dict (Story #925) when no elevation window
            # is active. Either outcome is valid — the invariant is no AttributeError.
            if "content" in result:
                content = json.loads(result["content"][0]["text"])
                if "error" in content:
                    assert (
                        "has no attribute 'api_key_manager'" not in content["error"]
                    ), "Should not have AttributeError for api_key_manager"
            else:
                assert "error" in result, f"Unexpected response format: {result}"
                assert "has no attribute 'api_key_manager'" not in result.get(
                    "error", ""
                ), "Should not have AttributeError for api_key_manager"
                assert "has no attribute 'api_key_manager'" not in result.get(
                    "message", ""
                ), "Should not have AttributeError for api_key_manager"
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")

    def test_list_mcp_credentials_handler_no_attribute_error(self, normal_user):
        """
        Verify list_mcp_credentials(scope='self') does NOT raise AttributeError.

        Story #989: scope parameter now required; self-scope has no elevation gate.
        """
        handler = HANDLER_REGISTRY["list_mcp_credentials"]

        try:
            result = handler({"scope": "self"}, normal_user)
            assert "content" in result, "Handler should return MCP-compliant response"
            content = json.loads(result["content"][0]["text"])
            if "error" in content:
                assert (
                    "has no attribute 'mcp_credential_manager'" not in content["error"]
                ), "Should not have AttributeError for mcp_credential_manager"
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")

    def test_manage_mcp_credential_create_no_attribute_error(self, normal_user):
        """
        Verify manage_mcp_credential(action='create') does NOT raise AttributeError.

        Story #989: unified handler replaces create_mcp_credential.
        Elevation-gated; may return error dict when no window is active.
        Either outcome is valid — the key invariant is no AttributeError.
        """
        handler = HANDLER_REGISTRY["manage_mcp_credential"]

        try:
            result = handler({"action": "create", "description": "Test"}, normal_user)
            _assert_no_mcp_credential_manager_attribute_error(result)
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")

    def test_manage_mcp_credential_delete_no_attribute_error(self, normal_user):
        """
        Verify manage_mcp_credential(action='delete') does NOT raise AttributeError.

        Story #989: unified handler replaces delete_mcp_credential.
        Elevation-gated; may return error dict when no window is active.
        Either outcome is valid — the key invariant is no AttributeError.
        """
        handler = HANDLER_REGISTRY["manage_mcp_credential"]

        try:
            result = handler(
                {"action": "delete", "credential_id": "cred-123"}, normal_user
            )
            _assert_no_mcp_credential_manager_attribute_error(result)
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")


class TestAdminCredentialHandlersNoAttributeError:
    """
    Tests that verify unified admin credential handlers do NOT raise AttributeError.

    Story #989: Old admin_* handlers replaced by list_mcp_credentials (scope=user/all)
    and manage_mcp_credential (action=create/delete + target_user).
    """

    @pytest.fixture
    def admin_user(self):
        """Create an admin user for testing."""
        return User(
            username="admin_test",
            password_hash="$2b$12$hash",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )

    def test_list_user_scope_no_attribute_error(self, admin_user):
        """Verify list_mcp_credentials(scope='user') does NOT raise AttributeError."""
        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        try:
            result = handler({"scope": "user", "username": "testuser"}, admin_user)
            _assert_no_mcp_credential_manager_attribute_error(result)
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")

    def test_manage_mcp_credential_admin_create_no_attribute_error(self, admin_user):
        """Verify manage_mcp_credential(action='create', target_user=...) no AttributeError.

        Story #989: unified handler replaces admin_create_user_mcp_credential.
        Elevation-gated; may return error dict when no elevation window is active.
        Either outcome is valid — the key invariant is no AttributeError.
        """
        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        try:
            result = handler(
                {"action": "create", "target_user": "testuser", "description": "Test"},
                admin_user,
            )
            _assert_no_mcp_credential_manager_attribute_error(result)
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")

    def test_manage_mcp_credential_admin_delete_no_attribute_error(self, admin_user):
        """Verify manage_mcp_credential(action='delete', target_user=...) no AttributeError.

        Story #989: unified handler replaces admin_delete_user_mcp_credential.
        Elevation-gated; may return error dict when no elevation window is active.
        Either outcome is valid — the key invariant is no AttributeError.
        """
        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        try:
            result = handler(
                {
                    "action": "delete",
                    "target_user": "testuser",
                    "credential_id": "cred-123",
                },
                admin_user,
            )
            _assert_no_mcp_credential_manager_attribute_error(result)
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")

    def test_list_all_scope_no_attribute_error(self, admin_user):
        """Verify list_mcp_credentials(scope='all') does NOT raise AttributeError."""
        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        try:
            result = handler({"scope": "all"}, admin_user)
            _assert_no_mcp_credential_manager_attribute_error(result)
        except AttributeError as e:
            pytest.fail(f"Handler raised AttributeError: {e}")
