"""Stored secrets are written as authenticated ``v2:`` ciphertext under the
random key; legacy (unprefixed AES-CBC) values stay readable through every
legacy key derivation so they can be rewritten."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List
from unittest import mock

import pytest
from cryptography.fernet import Fernet

from code_indexer.server.services.token_encryption import (
    V2_PREFIX,
    StoredSecretKeyMismatch,
    StoredSecretUnreadable,
    decrypt_v2,
    decrypt_with_fallback,
    derive_key_from_salt,
    encrypt_token,
    encrypt_v2,
    is_v2,
    legacy_candidate_keys,
)

TOKEN = "ghp_" + "a" * 36


def test_v2_round_trip_and_prefix() -> None:
    key = Fernet.generate_key()
    value = encrypt_v2(TOKEN, key)
    assert value.startswith(V2_PREFIX)
    assert is_v2(value)
    assert TOKEN not in value
    assert decrypt_v2(value, key) == TOKEN


def test_v2_wrong_key_raises_typed_error() -> None:
    value = encrypt_v2(TOKEN, Fernet.generate_key())
    with pytest.raises(StoredSecretKeyMismatch):
        decrypt_v2(value, Fernet.generate_key())


def test_v2_tampered_ciphertext_is_rejected() -> None:
    key = Fernet.generate_key()
    value = encrypt_v2(TOKEN, key)
    body = value[len(V2_PREFIX) :]
    flipped = body[:-6] + ("A" if body[-6] != "A" else "B") + body[-5:]
    with pytest.raises(StoredSecretKeyMismatch):
        decrypt_v2(V2_PREFIX + flipped, key)


def test_legacy_value_is_not_v2() -> None:
    assert not is_v2(encrypt_token(TOKEN, derive_key_from_salt("x")))


def _server_dir(tmp_path: Path, salt: str = "", jwt: str = "") -> Path:
    if salt:
        (tmp_path / ".encryption_key_salt").write_text(salt)
    if jwt:
        (tmp_path / ".jwt_secret").write_text(jwt + "\n")
    return tmp_path


@pytest.mark.parametrize("source", ["salt_file", "cluster", "jwt_file", "hostname"])
def test_legacy_value_from_each_derivation_is_read_and_flagged(
    tmp_path: Path, source: str
) -> None:
    server_dir = _server_dir(tmp_path, salt="salt-content", jwt="node-jwt-secret")
    salt_input = {
        "salt_file": "salt-content",
        "cluster": "shared-cluster-jwt",
        "jwt_file": "node-jwt-secret",
        "hostname": os.uname().nodename,
    }[source]
    legacy = encrypt_token(TOKEN, derive_key_from_salt(salt_input))
    plaintext, is_legacy = decrypt_with_fallback(
        legacy,
        Fernet.generate_key(),
        lambda: legacy_candidate_keys(server_dir, "shared-cluster-jwt"),
        "test",
    )
    assert plaintext == TOKEN
    assert is_legacy is True


def test_v2_value_is_read_with_v2_key_and_not_flagged() -> None:
    key = Fernet.generate_key()
    calls: List[int] = []

    def candidates() -> List[bytes]:
        calls.append(1)
        return []

    plaintext, is_legacy = decrypt_with_fallback(
        encrypt_v2(TOKEN, key), key, candidates, "test"
    )
    assert (plaintext, is_legacy) == (TOKEN, False)
    assert calls == []  # legacy keys are only derived for legacy values


def test_v2_value_under_another_key_raises_mismatch() -> None:
    value = encrypt_v2(TOKEN, Fernet.generate_key())
    with pytest.raises(StoredSecretKeyMismatch):
        decrypt_with_fallback(value, Fernet.generate_key(), lambda: [], "test")


def test_legacy_value_no_candidate_reads_raises_unreadable(tmp_path: Path) -> None:
    legacy = encrypt_token(TOKEN, derive_key_from_salt("some-other-node"))
    with pytest.raises(StoredSecretUnreadable):
        decrypt_with_fallback(
            legacy,
            Fernet.generate_key(),
            lambda: legacy_candidate_keys(tmp_path, None),
            "test",
        )


def test_legacy_value_readable_under_two_candidates_is_refused() -> None:
    from code_indexer.server.services.token_encryption import StoredSecretAmbiguous

    key = derive_key_from_salt("collision-example")
    legacy = encrypt_token(TOKEN, key)
    with pytest.raises(StoredSecretAmbiguous) as exc:
        decrypt_with_fallback(legacy, Fernet.generate_key(), lambda: [key, key], "t")
    assert isinstance(exc.value, StoredSecretUnreadable)
    assert TOKEN not in str(exc.value)


def test_sweep_keeps_and_marks_an_ambiguous_legacy_row(tmp_path: Path) -> None:
    from code_indexer.server.services.stored_secret_key import StoredSecretCipher
    from code_indexer.server.services.stored_secret_sweep import sweep_stored_secrets
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
        CITokensSqliteBackend,
    )

    key = derive_key_from_salt("collision-example")

    class _CollidingCipher(StoredSecretCipher):
        def _legacy_keys(self) -> List[bytes]:
            return [key, key]

    db = str(tmp_path / "server.db")
    DatabaseSchema(db).initialize_database()
    backend = CITokensSqliteBackend(db)
    legacy = encrypt_token(TOKEN, key)
    backend.save_token("github", legacy, None)
    counts = sweep_stored_secrets(
        _CollidingCipher(tmp_path, "t"), [("ci_tokens", backend)]
    )
    assert counts["ci_tokens"].unreadable == 1
    stored = backend.get_token("github")
    assert stored is not None and stored["encrypted_token"] == legacy
    assert [m["id"] for m in backend.list_needs_reentry()] == ["github"]


def test_candidates_dedupe_and_skip_missing_sources(tmp_path: Path) -> None:
    host = os.uname().nodename
    _server_dir(tmp_path, salt=host)
    keys = legacy_candidate_keys(tmp_path, None)
    assert keys == [derive_key_from_salt(host)]


def test_candidate_order_prefers_salt_file(tmp_path: Path) -> None:
    _server_dir(tmp_path, salt="s", jwt="j")
    with mock.patch(
        "os.uname",
        return_value=os.uname_result(("L", "h.example.com", "r", "v", "m")),
    ):
        keys = legacy_candidate_keys(tmp_path, "c")
    assert keys == [
        derive_key_from_salt("s"),
        derive_key_from_salt("c"),
        derive_key_from_salt("j"),
        derive_key_from_salt("h.example.com"),
    ]
