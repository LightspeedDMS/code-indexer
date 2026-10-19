"""SSO never provisions, links or signs in the analysis agents' account.

Real UserManager (SQLite) and real OIDCManager; the IdP round-trip itself is
out of scope, as in the sibling OIDC tests (match_or_create_user directly).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.analysis_agent_principal import (
    ANALYSIS_AGENT_USERNAME,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.utils.config_manager import OIDCProviderConfig

PASSWORD = "Ex4mple#Strong!Pw9QzR"
LINKED_AT = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def users(tmp_path: Path) -> UserManager:
    db = str(tmp_path / "users.db")
    DatabaseSchema(db).initialize_database()
    return UserManager(use_sqlite=True, db_path=db)


async def _manager(users: UserManager, tmp_path: Path) -> OIDCManager:
    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )
    manager = OIDCManager(config, users, None)
    manager.db_path = str(tmp_path / "oidc.db")
    await manager._init_db()
    return manager


def _info(subject: str) -> OIDCUserInfo:
    return OIDCUserInfo(
        subject=subject,
        email="agent@example.com",
        email_verified=True,
        username=ANALYSIS_AGENT_USERNAME,
    )


@pytest.mark.asyncio
async def test_jit_provisioning_refuses_the_reserved_name(
    users: UserManager, tmp_path: Path
) -> None:
    manager = await _manager(users, tmp_path)
    assert await manager.match_or_create_user(_info("subject-jit")) is None
    assert users.get_user(ANALYSIS_AGENT_USERNAME) is None


def test_create_oidc_user_refuses_the_reserved_name(users: UserManager) -> None:
    with pytest.raises(ValueError):
        users.create_oidc_user(
            username=ANALYSIS_AGENT_USERNAME,
            role=UserRole.NORMAL_USER,
            email=None,
            oidc_identity={"subject": "subject-direct"},
        )
    assert users.get_user(ANALYSIS_AGENT_USERNAME) is None


@pytest.mark.asyncio
async def test_linking_refuses_the_reserved_name(
    users: UserManager, tmp_path: Path
) -> None:
    users.create_user(ANALYSIS_AGENT_USERNAME, PASSWORD, UserRole.NORMAL_USER)
    manager = await _manager(users, tmp_path)
    with pytest.raises(ValueError):
        await manager.link_oidc_identity(
            ANALYSIS_AGENT_USERNAME, "subject-link", "agent@example.com"
        )


@pytest.mark.asyncio
async def test_an_existing_link_never_yields_an_sso_session(
    users: UserManager, tmp_path: Path
) -> None:
    users.create_user(ANALYSIS_AGENT_USERNAME, PASSWORD, UserRole.NORMAL_USER)
    manager = await _manager(users, tmp_path)
    with closing(sqlite3.connect(manager.db_path)) as conn:
        conn.execute(
            "INSERT INTO oidc_identity_links (username, subject, email, linked_at, "
            "last_login) VALUES (?, ?, ?, ?, ?)",
            (ANALYSIS_AGENT_USERNAME, "subject-old", None, LINKED_AT, LINKED_AT),
        )
        conn.commit()
    assert await manager.match_or_create_user(_info("subject-old")) is None
