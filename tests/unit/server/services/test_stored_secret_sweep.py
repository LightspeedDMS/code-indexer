"""The startup sweep rewrites every legacy stored secret this node can read
as v2, keeps and marks the ones it cannot, and is idempotent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Tuple

from code_indexer.server.services.stored_secret_key import StoredSecretCipher
from code_indexer.server.services.stored_secret_sweep import sweep_stored_secrets
from code_indexer.server.services.token_encryption import (
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

TOKEN = "glpat-" + "c" * 24


class _GateClosedCipher(StoredSecretCipher):
    def v2_writes_allowed(self) -> bool:
        return False


def _setup(tmp_path: Path) -> Tuple[Path, Any, Any]:
    db = str(tmp_path / "server.db")
    DatabaseSchema(db).initialize_database()
    ci = CITokensSqliteBackend(db)
    git = GitCredentialsSqliteBackend(db)
    host_key = derive_key_from_salt(os.uname().nodename)
    foreign_key = derive_key_from_salt("another-node.example")
    ci.save_token("gitlab", encrypt_token(TOKEN, host_key), None)
    ci.save_token("github", encrypt_token(TOKEN, foreign_key), None)
    git.upsert_credential(
        credential_id="cred-1",
        username="alice",
        forge_type="gitlab",
        forge_host="git.example.com",
        encrypted_token=encrypt_token(TOKEN, host_key),
    )
    return tmp_path, ci, git


def _values(backend: Any) -> dict:
    return {r["id"]: r["encrypted_token"] for r in backend.list_secret_rows()}


def test_sweep_rewrites_readable_legacy_rows_and_marks_unreadable(
    tmp_path: Path,
) -> None:
    server_dir, ci, git = _setup(tmp_path)
    foreign = _values(ci)["github"]
    cipher = StoredSecretCipher(server_dir, "sweep")
    counts = sweep_stored_secrets(cipher, [("ci_tokens", ci), ("git", git)])
    assert (counts["ci_tokens"].rewritten, counts["ci_tokens"].unreadable) == (1, 1)
    assert (counts["git"].rewritten, counts["git"].unreadable) == (1, 0)
    assert _values(ci)["gitlab"].startswith("v2:")
    assert _values(ci)["github"] == foreign  # kept, never deleted
    assert [m["id"] for m in ci.list_needs_reentry()] == ["github"]
    assert cipher.decrypt(_values(git)["cred-1"]) == (TOKEN, False)


def test_sweep_is_idempotent(tmp_path: Path) -> None:
    server_dir, ci, git = _setup(tmp_path)
    cipher = StoredSecretCipher(server_dir, "sweep")
    sweep_stored_secrets(cipher, [("ci_tokens", ci), ("git", git)])
    before = (_values(ci), _values(git))
    counts = sweep_stored_secrets(cipher, [("ci_tokens", ci), ("git", git)])
    assert (_values(ci), _values(git)) == before
    assert counts["ci_tokens"].rewritten == 0
    assert counts["ci_tokens"].already_v2 == 1
    assert counts["ci_tokens"].unreadable == 1


def test_sweep_clears_mark_when_row_becomes_readable(tmp_path: Path) -> None:
    server_dir, ci, _ = _setup(tmp_path)
    cipher = StoredSecretCipher(server_dir, "sweep")
    sweep_stored_secrets(cipher, [("ci_tokens", ci)])
    assert ci.list_needs_reentry() != []
    # the node that wrote it shares its salt file; the row is readable now
    (server_dir / ".encryption_key_salt").write_text("another-node.example")
    sweep_stored_secrets(StoredSecretCipher(server_dir, "sweep"), [("ci_tokens", ci)])
    assert ci.list_needs_reentry() == []
    assert all(v.startswith("v2:") for v in _values(ci).values())


def test_sweep_with_gate_closed_rewrites_nothing(tmp_path: Path) -> None:
    server_dir, ci, git = _setup(tmp_path)
    before = (_values(ci), _values(git))
    cipher = _GateClosedCipher(server_dir, "sweep")
    counts = sweep_stored_secrets(cipher, [("ci_tokens", ci), ("git", git)])
    assert (_values(ci), _values(git)) == before
    assert counts["ci_tokens"].remaining == 1
    assert counts["git"].remaining == 1
