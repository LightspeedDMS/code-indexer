"""
OIDC just-in-time provisioning accepts display-style usernames claimed by
the IdP (embedded spaces, accented letters, apostrophes, non-Latin
scripts) and rejects only path hazards.

Uses a REAL UserManager (SQLite backend) so the real
validate_username_path_safe gate is exercised on every provisioning call.

Foundation #1 compliant: real UserManager, real SQLite backend, real
OIDCManager. Only the IdP HTTP round-trip itself is out of scope (JIT
provisioning is exercised directly via match_or_create_user, as the
existing OIDC front-door characterization tests in this suite do).
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def real_user_manager():
    from code_indexer.server.auth.user_manager import UserManager
    from code_indexer.server.storage.database_manager import DatabaseSchema

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "users.db")
        DatabaseSchema(db_path).initialize_database()
        yield UserManager(use_sqlite=True, db_path=db_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claimed_username",
    [
        "john smith",
        "josé",
        "o'brien",
        "李",
    ],
)
async def test_jit_provisioning_accepts_display_style_idp_claim(
    real_user_manager, tmp_path, claimed_username
):
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )

    manager = OIDCManager(config, real_user_manager, None)
    manager.db_path = str(tmp_path / "test_oidc.db")
    await manager._init_db()

    user_info = OIDCUserInfo(
        subject=f"subject-for-{claimed_username}",
        email="someone@example.com",
        email_verified=True,
        username=claimed_username,
    )

    user = await manager.match_or_create_user(user_info)

    assert user is not None, (
        f"JIT provisioning must accept the IdP-claimed username "
        f"{claimed_username!r} -- it is a single path component"
    )
    assert user.username == claimed_username
    assert real_user_manager.get_user(claimed_username) is not None
