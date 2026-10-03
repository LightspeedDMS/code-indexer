"""Account rows created by server components carry a trusted system actor.

A system row is built only by the system builder: ``actor_is_system`` is 1,
the actor is ``system:<component>`` and the source is ``system``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
    make_user_manager,
)
from code_indexer.server.auth.user_manager import UserRole


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "groups.db")


def test_sso_provisioning_records_a_system_actor(store, tmp_path: Path) -> None:
    users = make_user_manager(tmp_path)
    users.create_oidc_user(
        username="example-sso-user",
        role=UserRole.NORMAL_USER,
        email="person@example.com",
        oidc_identity={"subject": "example-subject"},
    )
    (row,) = store.rows("user_created")
    assert (row.actor, row.actor_is_system, row.source) == (
        "system:sso-provisioning",
        1,
        "system",
    )
    assert (row.target_id, row.outcome) == ("example-sso-user", "success")
    assert row.details == {"role": "normal_user", "provisioning": "sso"}
    assert "person@example.com" not in store.all_raw_text()


async def test_sso_jit_provisioning_writes_its_row_off_the_event_loop(
    store, tmp_path: Path, caplog
) -> None:
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    config = OIDCProviderConfig(
        enabled=True,
        enable_jit_provisioning=True,
        default_role="normal_user",
        username_claim="preferred_username",
    )
    manager = OIDCManager(config, make_user_manager(tmp_path), None)
    manager.db_path = str(tmp_path / "oidc.db")
    await manager._init_db()
    user = await manager.match_or_create_user(
        OIDCUserInfo(
            subject="example-subject",
            email="person@example.com",
            email_verified=True,
            username="example-jit-user",
        )
    )
    assert user is not None
    assert capture_errors(caplog) == []
    (row,) = store.rows("user_created")
    assert (row.actor, row.target_id, row.outcome) == (
        "system:sso-provisioning",
        "example-jit-user",
        "success",
    )


def test_self_registration_front_door_records_a_system_actor(
    store, tmp_path: Path, monkeypatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.jwt_manager import JWTManager
    from code_indexer.server.middleware.audit_request_context import (
        AuditRequestContextMiddleware,
    )
    from code_indexer.server.routers import inline_auth

    # The toggle lookup is configuration plumbing, not what is under test.
    monkeypatch.setattr(inline_auth, "_is_self_registration_enabled", lambda: True)
    users = make_user_manager(tmp_path)
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    inline_auth.register_auth_routes(
        app,
        jwt_manager=JWTManager(secret_key="example-jwt-secret"),
        user_manager=users,
        refresh_token_manager=None,
    )
    password = "SecureP@ssw0rd!XyZ789"
    response = TestClient(app).post(
        "/auth/register",
        json={
            "username": "example-registrant",
            "email": "person@example.com",
            "password": password,
        },
    )
    assert response.status_code == 200, response.text
    (row,) = store.rows("user_created")
    assert (row.actor, row.actor_is_system, row.source, row.target_id) == (
        "system:self-registration",
        1,
        "system",
        "example-registrant",
    )
    assert row.details == {"role": "normal_user", "provisioning": "self_registration"}
    text = store.all_raw_text()
    assert password not in text and "person@example.com" not in text


def test_mcp_self_registration_credential_is_a_system_actor(
    store, tmp_path: Path
) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )
    from code_indexer.server.services.config_service import ConfigService

    users = make_user_manager(tmp_path)
    users.create_user("admin", "SecureP@ssw0rd!XyZ789", UserRole.ADMIN)
    (tmp_path / "server").mkdir()
    # the production wiring (service_init): the ConfigService itself
    config_service = ConfigService(server_dir_path=str(tmp_path / "server"))
    config_service.load_config()  # writes the default config.json
    service = MCPSelfRegistrationService(config_service, MCPCredentialManager(users))
    creds = service.get_or_create_credentials()
    assert creds is not None
    (row,) = store.rows("mcp_credential_created")
    assert (row.actor, row.actor_is_system, row.outcome) == (
        "system:mcp-self-registration",
        1,
        "success",
    )
    assert row.details["for_self"] is False
    assert creds["client_secret"] not in store.all_raw_text()


def test_rejected_sso_provisioning_records_a_failure(store, tmp_path: Path) -> None:
    users = make_user_manager(tmp_path)
    with pytest.raises(ValueError):
        users.create_oidc_user(
            username="../bad",
            role=UserRole.NORMAL_USER,
            email=None,
            oidc_identity={"subject": "example-subject"},
        )
    (row,) = store.rows("user_created")
    assert (row.actor, row.outcome, row.target_id) == (
        "system:sso-provisioning",
        "failure",
        "(unknown)",
    )
