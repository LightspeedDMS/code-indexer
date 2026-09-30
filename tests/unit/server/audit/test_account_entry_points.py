"""Audited account entry points record one outcome row per call.

Real UserManager (SQLite backend), real audit store bound as the sink.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import AuditStore, bound_audit_store, make_user_manager
from code_indexer.server.auth.user_manager import (
    SSOPasswordChangeError,
    UserManager,
    UserRole,
)
from code_indexer.server.services.audit_events import SystemComponent

_PASSWORD = "SecureP@ssw0rd!XyZ789"
_ADMIN = "example-admin"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "groups.db")


@pytest.fixture()
def users(tmp_path: Path) -> UserManager:
    manager: UserManager = make_user_manager(tmp_path)
    manager.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    return manager


def test_create_user_records_role_and_admin_provisioning(store, users) -> None:
    users.create_user_audited(
        "example-user", _PASSWORD, UserRole.POWER_USER, actor=_ADMIN
    )
    (row,) = store.rows("user_created")
    assert (row.actor, row.target_type, row.target_id, row.outcome) == (
        _ADMIN,
        "user",
        "example-user",
        "success",
    )
    assert row.details == {"role": "power_user", "provisioning": "admin"}


def test_create_user_failure_records_failure_and_reraises(store, users) -> None:
    with pytest.raises(ValueError):
        users.create_user_audited("example-user", "weak", UserRole.ADMIN, actor=_ADMIN)
    (row,) = store.rows("user_created")
    # The account was never persisted: the typed name is not recorded.
    assert (row.outcome, row.target_id) == ("failure", "(unknown)")
    assert "weak" not in (row.raw_details or "")


def test_self_registration_is_a_system_actor(store, users) -> None:
    users.create_user_audited(
        "example-user",
        _PASSWORD,
        UserRole.NORMAL_USER,
        actor=SystemComponent.SELF_REGISTRATION,
    )
    (row,) = store.rows("user_created")
    assert (row.actor, row.actor_is_system) == ("system:self-registration", 1)
    assert row.details["provisioning"] == "self_registration"


def test_delete_user_records_deleted_role(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    assert users.delete_user_audited("example-user", actor=_ADMIN) is True
    (row,) = store.rows("user_deleted")
    assert (row.actor, row.target_id, row.outcome) == (
        _ADMIN,
        "example-user",
        "success",
    )
    assert row.details == {"deleted_role": "normal_user"}


def test_delete_missing_user_records_failure(store, users) -> None:
    assert users.delete_user_audited("example-missing", actor=_ADMIN) is False
    (row,) = store.rows("user_deleted")
    assert (row.outcome, row.target_id, row.details) == ("failure", "(unknown)", {})
    assert "example-missing" not in store.all_raw_text()


def test_role_change_of_missing_user_records_the_placeholder(store, users) -> None:
    assert not users.update_user_role_audited(
        "example-missing", UserRole.ADMIN, actor=_ADMIN
    )
    (row,) = store.rows("user_role_changed")
    assert (row.outcome, row.target_id, row.details) == ("failure", "(unknown)", {})


def _drop_table(db_path: str, table: str) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
    finally:
        conn.close()


def test_failing_account_lookup_still_records_a_failure_row(
    store, users, tmp_path: Path
) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    _drop_table(str(tmp_path / "cidx_server.db"), "users")
    with pytest.raises(Exception):
        users.delete_user_audited("example-user", actor=_ADMIN)
    (row,) = store.rows("user_deleted")
    assert (row.outcome, row.target_id, row.details) == ("failure", "(unknown)", {})


def test_role_change_records_old_and_new_role(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    users.update_user_role_audited("example-user", UserRole.ADMIN, actor=_ADMIN)
    (row,) = store.rows("user_role_changed")
    assert row.details == {"old_role": "normal_user", "new_role": "admin"}
    assert (row.actor, row.target_id, row.outcome) == (
        _ADMIN,
        "example-user",
        "success",
    )


def test_admin_password_reset_records_one_row_without_the_password(
    store, users
) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    new_password = "Another$ecureP4ss!Qw"
    users.change_password_audited("example-user", new_password, actor=_ADMIN)
    (row,) = store.rows("user_password_reset_by_admin")
    assert (row.actor, row.target_id, row.outcome) == (
        _ADMIN,
        "example-user",
        "success",
    )
    assert new_password not in store.all_raw_text()


def test_self_password_change_writes_no_new_row(store, users) -> None:
    users.change_password_audited(_ADMIN, "Another$ecureP4ss!Qw", actor=_ADMIN)
    assert store.rows("user_password_reset_by_admin") == []


def test_admin_password_reset_failure_records_failure(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    with pytest.raises(ValueError):
        users.change_password_audited("example-user", "weak", actor=_ADMIN)
    (row,) = store.rows("user_password_reset_by_admin")
    assert row.outcome == "failure"


def test_sso_password_reset_refusal_records_failure(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    users.set_oidc_identity("example-user", {"subject": "example-subject"})
    with pytest.raises(SSOPasswordChangeError):
        users.change_password_audited(
            "example-user", "Another$ecureP4ss!Qw", actor=_ADMIN
        )
    (row,) = store.rows("user_password_reset_by_admin")
    assert row.outcome == "failure"


def test_email_change_records_no_value(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    users.update_user_email_audited("example-user", "person@example.com", actor=_ADMIN)
    (row,) = store.rows("user_email_changed")
    assert (row.actor, row.target_id, row.outcome, row.details) == (
        _ADMIN,
        "example-user",
        "success",
        {},
    )
    assert "person@example.com" not in store.all_raw_text()


def test_unchanged_email_writes_no_row(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    users.update_user_email_audited("example-user", None, actor=_ADMIN)
    assert store.rows("user_email_changed") == []


def test_generate_api_key_records_the_id_never_the_key_or_name(store, users) -> None:
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    raw_key, key_id = ApiKeyManager(users).generate_key_audited(
        _ADMIN, name="free text name", actor=_ADMIN
    )
    (row,) = store.rows("api_key_created")
    assert (row.actor, row.target_type, row.target_id, row.outcome) == (
        _ADMIN,
        "api_key",
        key_id,
        "success",
    )
    assert row.details == {"key_id": key_id}
    text = store.all_raw_text()
    assert raw_key not in text and "free text name" not in text


def test_generate_api_key_failure_records_failure(store, users) -> None:
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    class _Broken:
        def add_api_key(self, **_kwargs):
            raise RuntimeError("store down")

    with pytest.raises(RuntimeError):
        ApiKeyManager(_Broken()).generate_key_audited(_ADMIN, actor=_ADMIN)  # type: ignore[arg-type]
    (row,) = store.rows("api_key_created")
    assert (row.outcome, row.target_id) == ("failure", "unresolved")


def test_mcp_credential_mint_for_self_and_for_another_user(store, users) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    manager = MCPCredentialManager(users)
    own = manager.generate_credential_audited(_ADMIN, "free text", actor=_ADMIN)
    other = manager.generate_credential_audited("example-user", None, actor=_ADMIN)
    rows = store.rows("mcp_credential_created")
    assert [(r.actor, r.target_id, r.outcome) for r in rows] == [
        (_ADMIN, own["credential_id"], "success"),
        (_ADMIN, other["credential_id"], "success"),
    ]
    assert rows[0].details == {"credential_id": own["credential_id"], "for_self": True}
    assert rows[1].details["for_self"] is False
    text = store.all_raw_text()
    for secret in (own["client_secret"], other["client_secret"], "free text"):
        assert secret not in text


def test_mcp_credential_mint_by_system_component(store, users) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    cred = MCPCredentialManager(users).generate_credential_audited(
        _ADMIN, "cidx-local-auto", actor=SystemComponent.MCP_SELF_REGISTRATION
    )
    (row,) = store.rows("mcp_credential_created")
    assert (row.actor, row.actor_is_system, row.target_id) == (
        "system:mcp-self-registration",
        1,
        cred["credential_id"],
    )
    assert row.details["for_self"] is False


def test_mcp_credential_mint_for_missing_user_records_failure(store, users) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    with pytest.raises(ValueError):
        MCPCredentialManager(users).generate_credential_audited(
            "example-missing", None, actor=_ADMIN
        )
    (row,) = store.rows("mcp_credential_created")
    assert row.outcome == "failure"


def test_mcp_credential_revoke(store, users) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    manager = MCPCredentialManager(users)
    cred = manager.generate_credential(_ADMIN, None)
    assert manager.revoke_credential_audited(
        _ADMIN, cred["credential_id"], actor=_ADMIN
    )
    assert not manager.revoke_credential_audited(
        _ADMIN, cred["credential_id"], actor=_ADMIN
    )
    rows = store.rows("mcp_credential_revoked")
    assert [(r.actor, r.target_id, r.outcome) for r in rows] == [
        (_ADMIN, cred["credential_id"], "success"),
        (_ADMIN, "unresolved", "failure"),
    ]
    assert rows[0].details == {"credential_id": cred["credential_id"], "for_self": True}
    assert rows[1].details == {}


def test_failed_revoke_never_records_the_supplied_id(store, users) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    secret_shaped = "mcp_sec_" + "7f" * 32
    assert not MCPCredentialManager(users).revoke_credential_audited(
        _ADMIN, secret_shaped, actor=_ADMIN
    )
    (row,) = store.rows("mcp_credential_revoked")
    assert (row.outcome, row.target_id, row.details) == ("failure", "unresolved", {})
    assert secret_shaped not in store.all_raw_text()


def _ssh_manager(tmp_path: Path):
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    return SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "meta" / "ssh_keys",
        config_path=ssh_dir / "config",
    )


def _fingerprint_id(fingerprint: str) -> str:
    """The server-derived key id: the SHA-256 fingerprint digest in hex."""
    import base64

    token = next(t for t in fingerprint.split() if t.startswith("SHA256:"))
    b64 = token[len("SHA256:") :]
    return "sha256:" + base64.b64decode(b64 + "=" * (-len(b64) % 4)).hex()


def test_ssh_key_lifecycle_rows_carry_the_fingerprint_never_the_name(
    store, tmp_path: Path
) -> None:
    manager = _ssh_manager(tmp_path)
    metadata = manager.create_key_audited(
        "example_key_name",
        key_type="ed25519",
        email="person@example.com",
        description="free text",
        actor=_ADMIN,
    )
    key_id = _fingerprint_id(metadata.fingerprint)
    manager.assign_key_to_host_audited(
        "example_key_name", "git.example.com", actor=_ADMIN
    )
    assert manager.delete_key_audited("example_key_name", actor=_ADMIN) is True
    rows = store.rows("ssh_key_")
    assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
        ("ssh_key_created", _ADMIN, key_id, "success"),
        ("ssh_key_host_assigned", _ADMIN, key_id, "success"),
        ("ssh_key_deleted", _ADMIN, key_id, "success"),
    ]
    assert rows[0].details == {"key_type": "ed25519"}
    assert rows[1].details == {"host": "git.example.com"}
    text = store.all_raw_text()
    for free_text in ("example_key_name", "free text", "person@example.com"):
        assert free_text not in text


def test_ssh_assign_to_invalid_host_records_failure_without_the_host(
    store, tmp_path: Path
) -> None:
    from code_indexer.server.services.ssh_input_validation import (
        InvalidHostnameError,
    )

    manager = _ssh_manager(tmp_path)
    metadata = manager.create_key("example_key")
    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host_audited(
            "example_key", "bad host\nProxyCommand x", actor=_ADMIN
        )
    (row,) = store.rows("ssh_key_host_assigned")
    assert (row.outcome, row.target_id) == (
        "failure",
        _fingerprint_id(metadata.fingerprint),
    )
    assert row.details == {}
    text = store.all_raw_text()
    assert "ProxyCommand" not in text and "example_key" not in text


def test_ssh_delete_of_unknown_key_records_the_placeholder(
    store, tmp_path: Path
) -> None:
    manager = _ssh_manager(tmp_path)
    manager.delete_key_audited("never_created_key", actor=_ADMIN)
    (row,) = store.rows("ssh_key_deleted")
    assert row.target_id == "unresolved"
    assert "never_created_key" not in store.all_raw_text()


def test_ssh_create_failure_records_failure(store, tmp_path: Path) -> None:
    manager = _ssh_manager(tmp_path)
    with pytest.raises(Exception):
        manager.create_key_audited("../escape", actor=_ADMIN)
    (row,) = store.rows("ssh_key_created")
    assert (row.outcome, row.target_id) == ("failure", "unresolved")


class _FakeForgeClient:
    """Stands in for the external forge API (network boundary)."""

    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    async def validate_and_discover(self, token: str, host: str):
        if not self.valid:
            raise PermissionError("forge rejected the token")
        return {"git_user_name": "Example", "forge_username": "example"}


def _git_manager(tmp_path: Path):
    from code_indexer.server.services.git_credential_manager import (
        GitCredentialManager,
    )
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "creds.db")
    DatabaseSchema(db_path=db_path).initialize_database()
    return GitCredentialManager(db_path=db_path)


async def test_git_credential_configure_and_delete(
    store, tmp_path: Path, monkeypatch, caplog
) -> None:
    import logging

    from _audit_accounts_support import CAPTURE_LOGGER, capture_errors
    from code_indexer.server.services import git_credential_manager as gcm

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    monkeypatch.setattr(gcm, "get_forge_client", lambda _t: _FakeForgeClient())
    manager = _git_manager(tmp_path)
    token = "ghp_exampleTokenValue0000000000000000"
    result = await manager.configure_credential_audited(
        "example-user",
        "github",
        "github.com",
        token,
        name="free text",
        actor="example-user",
    )
    credential_id = result["credential_id"]
    import anyio

    await anyio.to_thread.run_sync(
        lambda: manager.delete_credential_audited(
            "example-user", credential_id, actor="example-user"
        )
    )
    rows = store.rows("git_credential_")
    assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
        ("git_credential_configured", "example-user", credential_id, "success"),
        ("git_credential_deleted", "example-user", credential_id, "success"),
    ]
    for row in rows:
        assert row.details == {"platform": "github", "forge_host": "github.com"}
    text = store.all_raw_text()
    assert token not in text and "free text" not in text
    assert capture_errors(caplog) == []


async def test_git_credential_rejected_token_records_failure(
    store, tmp_path: Path, monkeypatch
) -> None:
    from code_indexer.server.services import git_credential_manager as gcm

    monkeypatch.setattr(gcm, "get_forge_client", lambda _t: _FakeForgeClient(False))
    with pytest.raises(PermissionError):
        await _git_manager(tmp_path).configure_credential_audited(
            "example-user", "gitlab", "gitlab.example.com", "tok", actor="example-user"
        )
    (row,) = store.rows("git_credential_configured")
    assert (row.outcome, row.target_id, row.details) == ("failure", "unresolved", {})


def test_git_credential_delete_of_unknown_id_records_failure(
    store, tmp_path: Path
) -> None:
    with pytest.raises(PermissionError):
        _git_manager(tmp_path).delete_credential_audited(
            "example-user", "4c7e1d2a-0000-4000-8000-00000000000f", actor="example-user"
        )
    (row,) = store.rows("git_credential_deleted")
    assert (row.outcome, row.target_id, row.details) == ("failure", "unresolved", {})
    assert "4c7e1d2a-0000-4000-8000-00000000000f" not in store.all_raw_text()


def test_failing_git_credential_lookup_still_records_a_failure_row(
    store, tmp_path: Path
) -> None:
    manager = _git_manager(tmp_path)
    _drop_table(str(tmp_path / "creds.db"), "user_git_credentials")
    with pytest.raises(Exception):
        manager.delete_credential_audited(
            "example-user", "4c7e1d2a-0000-4000-8000-00000000000e", actor="example-user"
        )
    (row,) = store.rows("git_credential_deleted")
    assert (row.outcome, row.target_id, row.details) == ("failure", "unresolved", {})


def test_tool_access_grant_and_revoke_rows(store, tmp_path: Path) -> None:
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    groups = GroupAccessManager(tmp_path / "groups.db")
    group = groups.create_group(name="example-group", description="")
    groups.set_tool_access("search_code", group.id, True, _ADMIN)
    groups.set_tool_access("search_code", group.id, False, _ADMIN)
    rows = store.rows("group_tool_access_")
    assert [(r.action_type, r.actor, r.target_type, r.target_id) for r in rows] == [
        ("group_tool_access_granted", _ADMIN, "group", str(group.id)),
        ("group_tool_access_revoked", _ADMIN, "group", str(group.id)),
    ]
    for row in rows:
        assert row.outcome == "success"
        assert row.details == {"tool_name": "search_code", "all_groups": False}


def test_tool_access_for_missing_group_records_failure(store, tmp_path: Path) -> None:
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    groups = GroupAccessManager(tmp_path / "groups.db")
    with pytest.raises(ValueError):
        groups.set_tool_access("search_code", 999999, True, _ADMIN)
    (row,) = store.rows("group_tool_access_")
    assert (row.outcome, row.target_id, row.details) == (
        "failure",
        "unresolved",
        {"all_groups": False},
    )


def _not_applying_groups(tmp_path: Path):
    """A manager whose (real, SQLite-mode) backend reports "not applied"."""
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    class _NotApplyingBackend(GroupAccessManager):
        def set_tool_access(self, tool_name, group_id, allowed, granted_by):  # type: ignore[no-untyped-def]
            return False

    backend = _NotApplyingBackend(tmp_path / "groups.db")
    group = backend.create_group(name="example-group", description="")
    return GroupAccessManager(tmp_path / "unused.db", storage_backend=backend), group


def test_tool_access_not_applied_by_backend_records_failure(
    store, tmp_path: Path
) -> None:
    groups, group = _not_applying_groups(tmp_path)
    assert groups.set_tool_access("search_code", group.id, True, _ADMIN) is False
    (row,) = store.rows("group_tool_access_")
    assert (row.outcome, row.target_id, row.details) == (
        "failure",
        "unresolved",
        {"all_groups": False},
    )


def test_tool_access_route_refuses_when_backend_did_not_apply(
    store, tmp_path: Path
) -> None:
    from datetime import datetime, timezone

    from fastapi import HTTPException

    from code_indexer.server.auth.user_manager import User
    from code_indexer.server.routers import groups as groups_router

    groups, group = _not_applying_groups(tmp_path)
    admin = User(
        username=_ADMIN,
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(HTTPException) as excinfo:
        groups_router.grant_tool_access(
            group_id=group.id,
            tool_name="search_code",
            current_user=admin,
            group_manager=groups,
        )
    assert excinfo.value.status_code == 500


def test_tool_access_all_groups_writes_one_row_per_group(store, tmp_path: Path) -> None:
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    groups = GroupAccessManager(tmp_path / "groups.db")
    first = groups.create_group(name="example-a", description="")
    second = groups.create_group(name="example-b", description="")
    affected = groups.set_tool_access_all_groups("search_code", False, _ADMIN)
    rows = store.rows("group_tool_access_")
    assert sorted(r.target_id for r in rows) == sorted(str(g) for g in affected)
    assert {str(first.id), str(second.id)} <= {r.target_id for r in rows}
    for row in rows:
        assert (row.action_type, row.actor, row.outcome) == (
            "group_tool_access_revoked",
            _ADMIN,
            "success",
        )
        assert row.details == {"tool_name": "search_code", "all_groups": True}


def test_delete_api_key_records_key_id(store, users) -> None:
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    key_id = "4c7e1d2a-0000-4000-8000-000000000003"
    users.add_api_key(
        "example-user",
        key_id,
        "hash",
        "cidx_sk_abcd",
        "free text name",
        "2026-01-01T00:00:00+00:00",
    )
    assert users.delete_api_key_audited("example-user", key_id, actor="example-user")
    (row,) = store.rows("api_key_deleted")
    assert (row.actor, row.target_type, row.target_id, row.outcome) == (
        "example-user",
        "api_key",
        key_id,
        "success",
    )
    assert row.details == {"key_id": key_id}
    assert "free text name" not in store.all_raw_text()


def test_failed_api_key_delete_never_records_the_supplied_id(store, users) -> None:
    """A caller-supplied id shaped like a raw API key is never stored."""
    users.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    raw_key_shaped = "cidx_sk_" + "0123456789abcdef" * 2
    assert not users.delete_api_key_audited(
        "example-user", raw_key_shaped, actor="example-user"
    )
    (row,) = store.rows("api_key_deleted")
    assert (row.outcome, row.target_id, row.details) == ("failure", "unresolved", {})
    assert raw_key_shaped not in store.all_raw_text()
