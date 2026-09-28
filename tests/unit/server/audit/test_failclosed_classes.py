"""The high-risk account and credential classes always proceed on audit failure.

By owner decision no action is refused when its audit row cannot be written:
the action completes with its normal result, the lost row is counted (the
``/health`` counter) and one ERROR line is logged without any secret.  The
classes are: MCP credential mint (for oneself and for another user), API key
creation, creating an admin user and promoting a user to admin, SSH key host
assignment, and an admin resetting another user's password.

The store is made unwritable for real (its table is renamed through a
separate connection), never mocked.
"""

from __future__ import annotations

import logging
import sqlite3
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
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services import audit_capture

_ADMIN = "example-admin"
_PASSWORD = "SecureP@ssw0rd!XyZ789"
_NEW_PASSWORD = "Another$ecureP4ss!Qw"


@pytest.fixture(autouse=True)
def fresh_reporter(monkeypatch):
    reporter = audit_capture._DropReporter()
    monkeypatch.setattr(audit_capture, "_reporter", reporter)
    return reporter


@pytest.fixture()
def broken_store(tmp_path: Path, caplog) -> Iterator[AuditStore]:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    for store in bound_audit_store(tmp_path / "groups.db"):
        conn = sqlite3.connect(str(store.db_path))
        try:
            conn.execute("ALTER TABLE audit_logs RENAME TO audit_logs_moved")
            conn.commit()
        finally:
            conn.close()
        yield store


@pytest.fixture()
def users(tmp_path: Path) -> UserManager:
    manager: UserManager = make_user_manager(tmp_path)
    manager.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    manager.create_user("example-user", _PASSWORD, UserRole.NORMAL_USER)
    return manager


def _assert_one_counted_drop(caplog, *secrets: str) -> None:
    assert audit_capture.records_dropped_since_boot() == 1
    errors = capture_errors(caplog)
    assert len(errors) == 1, errors
    for secret in secrets:
        assert secret not in errors[0]


def test_mcp_credential_mint_for_self_proceeds(broken_store, users, caplog) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    cred = MCPCredentialManager(users).generate_credential_audited(
        _ADMIN, "free text", actor=_ADMIN
    )
    assert (
        users.get_mcp_credentials(_ADMIN)[0]["credential_id"] == cred["credential_id"]
    )
    _assert_one_counted_drop(caplog, cred["client_secret"], "free text")


def test_mcp_credential_mint_for_another_user_proceeds(
    broken_store, users, caplog
) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    cred = MCPCredentialManager(users).generate_credential_audited(
        "example-user", None, actor=_ADMIN
    )
    assert [c["credential_id"] for c in users.get_mcp_credentials("example-user")] == [
        cred["credential_id"]
    ]
    _assert_one_counted_drop(caplog, cred["client_secret"])


def test_api_key_creation_proceeds(broken_store, users, caplog) -> None:
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    raw_key, key_id = ApiKeyManager(users).generate_key_audited(_ADMIN, actor=_ADMIN)
    assert users.validate_user_api_key(_ADMIN, raw_key) is not None
    assert [k["key_id"] for k in users.get_api_keys(_ADMIN)] == [key_id]
    _assert_one_counted_drop(caplog, raw_key)


def test_admin_user_creation_proceeds(broken_store, users, caplog) -> None:
    users.create_user_audited(
        "example-new-admin", _PASSWORD, UserRole.ADMIN, actor=_ADMIN
    )
    created = users.get_user("example-new-admin")
    assert created is not None and created.role is UserRole.ADMIN
    _assert_one_counted_drop(caplog, _PASSWORD)


def test_promotion_to_admin_proceeds(broken_store, users, caplog) -> None:
    assert users.update_user_role_audited("example-user", UserRole.ADMIN, actor=_ADMIN)
    promoted = users.get_user("example-user")
    assert promoted is not None and promoted.role is UserRole.ADMIN
    _assert_one_counted_drop(caplog)


def test_admin_password_reset_of_another_user_proceeds(
    broken_store, users, caplog
) -> None:
    assert users.change_password_audited("example-user", _NEW_PASSWORD, actor=_ADMIN)
    assert users.authenticate_user("example-user", _NEW_PASSWORD) is not None
    _assert_one_counted_drop(caplog, _NEW_PASSWORD)


def test_ssh_host_assignment_proceeds(broken_store, tmp_path: Path, caplog) -> None:
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    manager = SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "meta" / "ssh_keys",
        config_path=ssh_dir / "config",
    )
    manager.create_key("example_key")
    metadata = manager.assign_key_to_host_audited(
        "example_key", "git.example.com", actor=_ADMIN
    )
    assert metadata.hosts == ["git.example.com"]
    assert "git.example.com" in (ssh_dir / "config").read_text()
    _assert_one_counted_drop(caplog)
