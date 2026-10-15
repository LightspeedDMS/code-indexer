"""Git credentials and CI tokens are stored under the random stored-secret
key as v2 values; legacy values stay usable after an upgrade and converge to
v2; undecryptable values are kept and reported as needing re-entry."""

from __future__ import annotations

import binascii
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

import pytest

from code_indexer.server.services.ci_token_manager import create_token_manager
from code_indexer.server.services.git_credential_manager import (
    create_git_credential_manager,
)
from code_indexer.server.services.token_encryption import (
    decrypt_single,
    derive_key_from_salt,
    encrypt_token,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
    CITokensSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.git_credentials_backend import (
    GitCredentialsSqliteBackend,
)

GH_TOKEN = "ghp_" + "b" * 36
FORGE_HOST = "git.example.com"


def _server(tmp_path: Path) -> Tuple[str, str]:
    server_dir = tmp_path / "server"
    server_dir.mkdir(parents=True)
    db_path = str(server_dir / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return str(server_dir), db_path


class _ForgeStub:
    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
        return {"git_user_name": "Example", "git_user_email": "dev@example.com"}


async def _configure(server_dir: str, db_path: str) -> None:
    mgr = create_git_credential_manager(db_path, server_dir)
    with mock.patch(
        "code_indexer.server.services.git_credential_manager.get_forge_client",
        return_value=_ForgeStub(),
    ):
        await mgr.configure_credential("alice", "github", FORGE_HOST, GH_TOKEN)


def _ci_value(backend: CITokensSqliteBackend, platform: str) -> str:
    row = backend.get_token(platform)
    assert row is not None
    return str(row["encrypted_token"])


def _git_row(db_path: str) -> Dict[str, Any]:
    rows = GitCredentialsSqliteBackend(db_path).list_secret_rows()
    assert len(rows) == 1
    return rows[0]


def _host_keys(server_dir: str) -> List[bytes]:
    keys = [derive_key_from_salt(os.uname().nodename)]
    salt = Path(server_dir) / ".encryption_key_salt"
    if salt.exists():
        keys.append(derive_key_from_salt(salt.read_text()))
    return keys


def _readable_with(stored: str, key: bytes) -> bool:
    try:
        return decrypt_single(stored, key) == GH_TOKEN
    except (ValueError, binascii.Error):
        return False


def test_stored_ci_token_is_not_readable_with_host_derived_keys(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    create_token_manager(server_dir, db_path).save_token("github", GH_TOKEN)
    stored = _ci_value(CITokensSqliteBackend(db_path), "github")
    assert [_readable_with(stored, k) for k in _host_keys(server_dir)] == [False] * len(
        _host_keys(server_dir)
    )
    assert stored.startswith("v2:")


@pytest.mark.asyncio
async def test_git_credential_from_another_server_dir_on_same_host_is_unreadable(
    tmp_path: Path,
) -> None:
    a_dir, a_db = _server(tmp_path / "a")
    b_dir, _ = _server(tmp_path / "b")
    await _configure(a_dir, a_db)
    stored = _git_row(a_db)["encrypted_token"]
    other = create_git_credential_manager(a_db, b_dir)
    with pytest.raises(ValueError):
        other._cipher.decrypt(stored)


@pytest.mark.asyncio
async def test_git_credential_survives_restart(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    await _configure(server_dir, db_path)
    assert _git_row(db_path)["encrypted_token"].startswith("v2:")
    restarted = create_git_credential_manager(db_path, server_dir)
    cred = restarted.get_credential_for_host("alice", FORGE_HOST)
    assert cred is not None and cred["token"] == GH_TOKEN


def test_ci_token_survives_restart(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    create_token_manager(server_dir, db_path).save_token("github", GH_TOKEN)
    data = create_token_manager(server_dir, db_path).get_token("github")
    assert data is not None and data.token == GH_TOKEN


@pytest.mark.parametrize("legacy_salt", ["salt-file", "hostname"])
def test_legacy_git_credential_usable_after_upgrade_and_rewritten(
    tmp_path: Path, legacy_salt: str
) -> None:
    server_dir, db_path = _server(tmp_path)
    salt = os.uname().nodename
    if legacy_salt == "salt-file":
        salt = "pre-upgrade-salt-value"
        (Path(server_dir) / ".encryption_key_salt").write_text(salt)
    GitCredentialsSqliteBackend(db_path).upsert_credential(
        credential_id="cred-legacy",
        username="alice",
        forge_type="github",
        forge_host=FORGE_HOST,
        encrypted_token=encrypt_token(GH_TOKEN, derive_key_from_salt(salt)),
    )
    mgr = create_git_credential_manager(db_path, server_dir)
    cred = mgr.get_credential_for_host("alice", FORGE_HOST)
    assert cred is not None and cred["token"] == GH_TOKEN
    assert _git_row(db_path)["encrypted_token"].startswith("v2:")
    again = create_git_credential_manager(db_path, server_dir)
    cred_again = again.get_credential_for_host("alice", FORGE_HOST)
    assert cred_again is not None and cred_again["token"] == GH_TOKEN


def test_legacy_ci_token_usable_after_upgrade_and_rewritten(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    legacy = encrypt_token(GH_TOKEN, derive_key_from_salt(os.uname().nodename))
    CITokensSqliteBackend(db_path).save_token("github", legacy, None)
    data = create_token_manager(server_dir, db_path).get_token("github")
    assert data is not None and data.token == GH_TOKEN
    stored = _ci_value(CITokensSqliteBackend(db_path), "github")
    assert stored.startswith("v2:")


@pytest.mark.asyncio
async def test_successful_read_clears_a_needs_reentry_mark(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    await _configure(server_dir, db_path)
    create_token_manager(server_dir, db_path).save_token("github", GH_TOKEN)
    git_backend = GitCredentialsSqliteBackend(db_path)
    ci_backend = CITokensSqliteBackend(db_path)
    git_row = _git_row(db_path)
    git_backend.mark_needs_reentry(git_row["id"], git_row["encrypted_token"])
    ci_backend.mark_needs_reentry("github", _ci_value(ci_backend, "github"))
    assert git_backend.list_needs_reentry() and ci_backend.list_needs_reentry()
    mgr = create_git_credential_manager(db_path, server_dir)
    assert mgr.get_credential_for_host("alice", FORGE_HOST) is not None
    assert create_token_manager(server_dir, db_path).get_token("github") is not None
    assert git_backend.list_needs_reentry() == []
    assert ci_backend.list_needs_reentry() == []


def _record_clears(backend: Any) -> List[str]:
    calls: List[str] = []
    real = backend.clear_needs_reentry

    def recording(row_id: str, expected: str) -> bool:
        calls.append(row_id)
        return bool(real(row_id, expected))

    backend.clear_needs_reentry = recording
    return calls


@pytest.mark.asyncio
async def test_reading_an_unmarked_row_performs_no_clear_write(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    await _configure(server_dir, db_path)
    create_token_manager(server_dir, db_path).save_token("github", GH_TOKEN)
    git_mgr = create_git_credential_manager(db_path, server_dir)
    ci_mgr = create_token_manager(server_dir, db_path)
    git_calls = _record_clears(git_mgr._backend)
    ci_calls = _record_clears(ci_mgr._sqlite_backend)
    assert git_mgr.get_credential_for_host("alice", FORGE_HOST) is not None
    assert [e["needs_reentry"] for e in git_mgr.list_credentials("alice")] == [False]
    assert ci_mgr.get_token("github") is not None
    assert (git_calls, ci_calls) == ([], [])
    row = _git_row(db_path)
    git_mgr._backend.mark_needs_reentry(row["id"], row["encrypted_token"])
    ci_backend = CITokensSqliteBackend(db_path)
    ci_backend.mark_needs_reentry("github", _ci_value(ci_backend, "github"))
    git_mgr.get_credential_for_host("alice", FORGE_HOST)
    ci_mgr.get_token("github")
    assert (git_calls, ci_calls) == ([row["id"]], ["github"])
    assert git_mgr._backend.list_needs_reentry() == []
    assert ci_backend.list_needs_reentry() == []


def test_rewrite_never_overwrites_a_concurrently_changed_row(tmp_path: Path) -> None:
    server_dir, db_path = _server(tmp_path)
    backend = CITokensSqliteBackend(db_path)
    legacy = encrypt_token(GH_TOKEN, derive_key_from_salt(os.uname().nodename))
    backend.save_token("github", "v2:changed-by-another-request", None)
    mgr = create_token_manager(server_dir, db_path)
    plaintext = mgr._cipher.read(
        legacy,
        lambda old, new: backend.replace_encrypted_token_if_unchanged(
            "github", old, new
        ),
    )
    assert plaintext == GH_TOKEN
    stored = _ci_value(backend, "github")
    assert stored == "v2:changed-by-another-request"


def test_undecryptable_rows_are_kept_and_listed_as_needing_reentry(
    tmp_path: Path,
) -> None:
    server_dir, db_path = _server(tmp_path)
    foreign = encrypt_token(GH_TOKEN, derive_key_from_salt("another-node.example"))
    CITokensSqliteBackend(db_path).save_token("gitlab", foreign, None)
    GitCredentialsSqliteBackend(db_path).upsert_credential(
        credential_id="cred-x",
        username="alice",
        forge_type="github",
        forge_host=FORGE_HOST,
        encrypted_token=foreign,
    )
    ci = create_token_manager(server_dir, db_path)
    assert ci.get_token("gitlab") is None
    statuses = ci.list_tokens()  # always lists both known platforms
    assert statuses["gitlab"].needs_reentry is True
    assert statuses["github"].needs_reentry is False
    entries = create_git_credential_manager(db_path, server_dir).list_credentials(
        "alice"
    )
    assert [e["needs_reentry"] for e in entries] == [True]
    kept = _ci_value(CITokensSqliteBackend(db_path), "gitlab")
    assert kept == foreign
    assert _git_row(db_path)["encrypted_token"] == foreign
