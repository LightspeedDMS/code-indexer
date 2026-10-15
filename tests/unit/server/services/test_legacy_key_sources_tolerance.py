"""An unreadable legacy key source is skipped like a missing one (one
WARNING per source); a value is marked as needing re-entry only on a
definitive verdict; legacy-format writes use only a readable salt file."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterator, Tuple

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.encryption_key_salt import (
    ensure_encryption_key_salt,
)
from code_indexer.server.services.git_credential_manager import (
    create_git_credential_manager,
)
from code_indexer.server.services.stored_secret_key import (
    KEY_FILENAME,
    StoredSecretCipher,
)
from code_indexer.server.services.stored_secret_sweep import sweep_stored_secrets
from code_indexer.server.services.token_encryption import (
    StoredSecretLegacyWriteRefused,
    StoredSecretUndetermined,
    decrypt_single,
    decrypt_with_fallback,
    derive_key_from_salt,
    encrypt_token,
    encrypt_v2,
    legacy_candidate_keys,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
    CITokensSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.git_credentials_backend import (
    GitCredentialsSqliteBackend,
)

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads files regardless of mode"
)

TOKEN = "glpat-" + "k" * 24
JWT_VALUE = "node-local-jwt-example"
SALT_VALUE = "salt-example"


@pytest.fixture
def server_dir(tmp_path: Path) -> Iterator[Path]:
    """A server dir whose .jwt_secret the server user cannot read."""
    jwt = tmp_path / ".jwt_secret"
    jwt.write_text(JWT_VALUE + "\n")
    jwt.chmod(0o000)
    yield tmp_path
    jwt.chmod(0o600)


def _legacy(salt: str) -> str:
    return encrypt_token(TOKEN, derive_key_from_salt(salt))


def _host_legacy() -> str:
    return _legacy(os.uname().nodename)


def _db(server_dir: Path) -> str:
    db = str(server_dir / "cidx_server.db")
    DatabaseSchema(db).initialize_database()
    return db


def test_unreadable_source_is_skipped_and_others_still_decrypt(
    server_dir: Path,
) -> None:
    keys, complete = legacy_candidate_keys(server_dir, None)
    assert complete is False
    plaintext, is_legacy = decrypt_with_fallback(
        _host_legacy(), Fernet.generate_key(), lambda: keys, "test"
    )
    assert (plaintext, is_legacy) == (TOKEN, True)


def test_missing_sources_leave_the_candidate_set_complete(tmp_path: Path) -> None:
    assert legacy_candidate_keys(tmp_path, None)[1] is True


def test_one_warning_per_unreadable_source(
    server_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    legacy_candidate_keys(server_dir, None)
    legacy_candidate_keys(server_dir, None)
    warnings = [r for r in caplog.records if ".jwt_secret" in r.getMessage()]
    assert len(warnings) == 1
    assert "unreadable" in warnings[0].getMessage()
    assert JWT_VALUE not in warnings[0].getMessage()


def test_sweep_defers_a_row_whose_only_source_is_unreadable(
    server_dir: Path,
) -> None:
    backend = CITokensSqliteBackend(_db(server_dir))
    backend.save_token("github", _host_legacy(), None)
    backend.save_token("gitlab", _legacy(JWT_VALUE), None)
    counts = sweep_stored_secrets(
        StoredSecretCipher(server_dir, "sweep"), [("ci_tokens", backend)]
    )["ci_tokens"]
    assert (counts.rewritten, counts.unreadable, counts.errors) == (1, 0, 1)
    github = backend.get_token("github")
    assert github is not None and github["encrypted_token"].startswith("v2:")
    assert backend.list_needs_reentry() == []


def test_sweep_marks_when_every_source_was_read_and_none_matches(
    tmp_path: Path,
) -> None:
    backend = CITokensSqliteBackend(_db(tmp_path))
    backend.save_token("gitlab", _legacy("another-node.example"), None)
    counts = sweep_stored_secrets(
        StoredSecretCipher(tmp_path, "sweep"), [("ci_tokens", backend)]
    )["ci_tokens"]
    assert (counts.unreadable, counts.errors) == (1, 0)
    assert [m["id"] for m in backend.list_needs_reentry()] == ["gitlab"]


def _git_row(db: str, encrypted: str) -> None:
    GitCredentialsSqliteBackend(db).upsert_credential(
        credential_id="cred-1",
        username="alice",
        forge_type="gitlab",
        forge_host="git.example.com",
        encrypted_token=encrypted,
    )


def test_read_path_returns_plaintext_with_an_unreadable_source(
    server_dir: Path,
) -> None:
    db = _db(server_dir)
    _git_row(db, _host_legacy())
    cred = create_git_credential_manager(db, str(server_dir)).get_credential_for_host(
        "alice", "git.example.com"
    )
    assert cred is not None and cred["token"] == TOKEN


def test_read_path_leaves_an_undetermined_row_unmarked(server_dir: Path) -> None:
    db = _db(server_dir)
    _git_row(db, _legacy(JWT_VALUE))
    manager = create_git_credential_manager(db, str(server_dir))
    with pytest.raises(StoredSecretUndetermined):
        manager.get_credential_for_host("alice", "git.example.com")
    assert GitCredentialsSqliteBackend(db).list_needs_reentry() == []


def test_ci_token_read_path_returns_plaintext_with_an_unreadable_source(
    server_dir: Path,
) -> None:
    from code_indexer.server.services.ci_token_manager import CITokenManager

    db = _db(server_dir)
    CITokensSqliteBackend(db).save_token("github", _host_legacy(), None)
    manager = CITokenManager(
        server_dir_path=str(server_dir), use_sqlite=True, db_path=db
    )
    token = manager.get_token("github")
    assert token is not None and token.token == TOKEN


def test_ci_read_path_leaves_an_undetermined_row_unmarked(server_dir: Path) -> None:
    from code_indexer.server.services.ci_token_manager import CITokenManager

    db = _db(server_dir)
    CITokensSqliteBackend(db).save_token("gitlab", _legacy(JWT_VALUE), None)
    manager = CITokenManager(
        server_dir_path=str(server_dir), use_sqlite=True, db_path=db
    )
    assert manager.get_token("gitlab") is None
    assert CITokensSqliteBackend(db).list_needs_reentry() == []


class _BrokenPool:
    """A database pool that cannot hand out a connection."""

    def connection(self) -> None:
        raise ConnectionError("database unavailable")


def test_cluster_secret_read_failure_is_skipped(tmp_path: Path) -> None:
    cipher = StoredSecretCipher(tmp_path, "test")
    cipher._pool = _BrokenPool()  # the cluster_secrets read fails
    assert cipher.decrypt(_host_legacy()) == (TOKEN, True)


def test_unusable_fallback_key_file_leaves_the_value_undetermined(
    tmp_path: Path,
) -> None:
    cipher = StoredSecretCipher(tmp_path, "test")  # creates the key file
    cipher._pool = _BrokenPool()  # behaves as a cluster cipher
    cipher._v2_key = Fernet.generate_key()  # the cluster key
    (tmp_path / KEY_FILENAME).write_text("corrupt")
    with pytest.raises(StoredSecretUndetermined):
        cipher.decrypt(encrypt_v2(TOKEN, Fernet.generate_key()))


class _GateClosedCipher(StoredSecretCipher):
    def v2_writes_allowed(self) -> bool:
        return False


def test_legacy_write_uses_a_readable_salt_file(tmp_path: Path) -> None:
    (tmp_path / ".encryption_key_salt").write_text(SALT_VALUE)
    value = _GateClosedCipher(tmp_path, "test").encrypt(TOKEN)
    assert decrypt_single(value, derive_key_from_salt(SALT_VALUE)) == TOKEN


@pytest.mark.parametrize("salt_state", ["missing", "unreadable"])
def test_legacy_write_is_refused_without_a_readable_salt_file(
    tmp_path: Path, salt_state: str
) -> None:
    salt_file = tmp_path / ".encryption_key_salt"
    if salt_state == "unreadable":
        salt_file.write_text(SALT_VALUE)
        salt_file.chmod(0o000)
    try:
        with pytest.raises(StoredSecretLegacyWriteRefused):
            _GateClosedCipher(tmp_path, "test").encrypt(TOKEN)
    finally:
        if salt_file.exists():
            salt_file.chmod(0o600)


def test_salt_seeding_writes_nothing_when_jwt_secret_is_unreadable(
    server_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    assert ensure_encryption_key_salt(server_dir, "postgres") is None
    assert not (server_dir / ".encryption_key_salt").exists()
    assert JWT_VALUE not in caplog.text
    assert "unreadable" in caplog.text


@pytest.mark.parametrize("storage_mode", ["sqlite", "postgres"])
def test_existing_unreadable_salt_file_is_left_untouched(
    tmp_path: Path, storage_mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    salt_file = tmp_path / ".encryption_key_salt"
    salt_file.write_text(SALT_VALUE)
    salt_file.chmod(0o000)
    caplog.set_level(logging.WARNING)
    try:
        assert ensure_encryption_key_salt(tmp_path, storage_mode) is None
        assert ensure_encryption_key_salt(tmp_path, storage_mode) is None
    finally:
        salt_file.chmod(0o600)
    assert salt_file.read_text() == SALT_VALUE
    warnings = [r for r in caplog.records if str(salt_file) in r.getMessage()]
    assert len(warnings) == 1 and SALT_VALUE not in warnings[0].getMessage()


@pytest.mark.parametrize("storage_mode", ["sqlite", "postgres"])
def test_directory_at_the_salt_path_is_unreadable_and_left_alone(
    tmp_path: Path, storage_mode: str
) -> None:
    salt_path = tmp_path / ".encryption_key_salt"
    salt_path.mkdir()
    (salt_path / "marker").write_text("kept")
    assert ensure_encryption_key_salt(tmp_path, storage_mode) is None
    assert salt_path.is_dir() and (salt_path / "marker").read_text() == "kept"
    assert legacy_candidate_keys(tmp_path, None)[1] is False


class _FailingRowCipher(StoredSecretCipher):
    """Building candidates for one specific stored value fails unexpectedly."""

    failing_value = ""

    def decrypt(self, value: str) -> Tuple[str, bool]:
        if value == self.failing_value:
            raise RuntimeError("candidate source failed")
        return super().decrypt(value)


def test_sweep_continues_past_an_unexpected_row_failure_without_marking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    backend = CITokensSqliteBackend(_db(tmp_path))
    failing = _legacy("another-node.example")
    backend.save_token("gitlab", failing, None)
    backend.save_token("github", _host_legacy(), None)
    cipher = _FailingRowCipher(tmp_path, "sweep")
    cipher.failing_value = failing
    caplog.set_level(logging.ERROR)
    counts = sweep_stored_secrets(cipher, [("ci_tokens", backend)])
    assert counts["ci_tokens"].errors == 1
    assert counts["ci_tokens"].rewritten == 1
    assert backend.list_needs_reentry() == []
    gitlab = backend.get_token("gitlab")
    assert gitlab is not None and gitlab["encrypted_token"] == failing
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert TOKEN not in caplog.text


class _MarkFailsBackend(CITokensSqliteBackend):
    def mark_needs_reentry(self, *args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("database unavailable")


def test_a_failing_mark_counts_only_as_an_error(tmp_path: Path) -> None:
    backend = _MarkFailsBackend(_db(tmp_path))
    backend.save_token("gitlab", _legacy("another-node.example"), None)
    counts = sweep_stored_secrets(
        StoredSecretCipher(tmp_path, "sweep"), [("ci_tokens", backend)]
    )["ci_tokens"]
    assert (counts.unreadable, counts.errors) == (0, 1)
