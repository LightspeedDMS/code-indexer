"""
OIDC JIT provisioning must
gracefully handle UserManager.create_oidc_user rejecting an unsafe username
(the validate_username_path_safe gate)
instead of propagating an unhandled ValueError as a 500 to the SSO callback.

A real IdP-derived username is input this server does not control (the
IdP decides its value, and a username_claim may point at a free-text
field) -- create_oidc_user raising
ValueError here is a NORMAL, expected outcome that must be treated the
same as any other JIT-provisioning failure: log and return None so the
caller (oidc/routes.py) yields its ordinary access-denied response.
"""

from unittest.mock import Mock

import pytest


@pytest.mark.asyncio
async def test_jit_provisioning_gracefully_rejects_unsafe_username(tmp_path):
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )

    user_manager = Mock()
    user_manager.get_user.return_value = None
    user_manager.get_user_by_email.return_value = None
    # Simulates the REAL UserManager.create_oidc_user behavior
    # rejecting an unsafe username.
    user_manager.create_oidc_user.side_effect = ValueError(
        "Username cannot be '.' or '..'"
    )

    manager = OIDCManager(config, user_manager, None)
    manager.db_path = str(tmp_path / "test_oidc.db")
    await manager._init_db()

    user_info = OIDCUserInfo(
        subject="new-subject-456",
        email="attacker@example.com",
        email_verified=True,
        username="..",
    )

    # Must NOT raise -- must return None so the SSO callback yields a
    # normal access-denied response instead of an unhandled 500.
    user = await manager.match_or_create_user(user_info)

    assert user is None


@pytest.mark.asyncio
async def test_jit_provisioning_still_works_for_legitimate_username(tmp_path):
    """Regression: the try/except added around create_oidc_user must not
    swallow or alter the SUCCESS path."""
    from datetime import datetime, timezone

    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )

    user_manager = Mock()
    user_manager.get_user.return_value = None
    user_manager.get_user_by_email.return_value = None
    new_user = User(
        username="jdoe",
        password_hash="",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )
    user_manager.create_oidc_user.return_value = new_user

    manager = OIDCManager(config, user_manager, None)
    manager.db_path = str(tmp_path / "test_oidc.db")
    await manager._init_db()

    user_info = OIDCUserInfo(
        subject="new-subject-789",
        email="jdoe@example.com",
        email_verified=True,
        username="jdoe",
    )

    user = await manager.match_or_create_user(user_info)

    assert user is not None
    assert user.username == "jdoe"


@pytest.mark.asyncio
async def test_jit_provisioning_accepts_plus_addressed_email_username(tmp_path):
    """A real-world 'plus addressing' email-shaped
    username (e.g. an OIDC preferred_username claim from an enterprise IdP)
    must be accepted by JIT provisioning, not rejected by the path-safety
    gate -- '@'/'+' carry no path-traversal meaning."""
    from datetime import datetime, timezone

    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )

    user_manager = Mock()
    user_manager.get_user.return_value = None
    user_manager.get_user_by_email.return_value = None
    new_user = User(
        username="john+tag@example.com",
        password_hash="",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )
    user_manager.create_oidc_user.return_value = new_user

    manager = OIDCManager(config, user_manager, None)
    manager.db_path = str(tmp_path / "test_oidc.db")
    await manager._init_db()

    user_info = OIDCUserInfo(
        subject="new-subject-plus-tag",
        email="john+tag@example.com",
        email_verified=True,
        username="john+tag@example.com",
    )

    user = await manager.match_or_create_user(user_info)

    assert user is not None
    assert user.username == "john+tag@example.com"
    user_manager.create_oidc_user.assert_called_once()
