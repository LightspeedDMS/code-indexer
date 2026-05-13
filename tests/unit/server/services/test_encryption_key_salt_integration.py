"""
Integration tests for unified encryption key salt usage (Story #999 Steps 2 & 3).

Tests that CITokenManager and GitCredentialManager:
- (Step 2) Derive encryption key from .encryption_key_salt when server_dir is provided
- (Step 2) Two managers with same salt derive same key; different salts produce different keys
- (Step 3) Try-decrypt fallback: ciphertext encrypted with legacy hostname-derived key
  (no server_dir) is readable after switching to salt-based manager where salt != hostname

The "different salts produce different keys" tests are foundational because they prove
the fallback is actually exercised (i.e. the keys differ and yet decryption succeeds).
"""

import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from src.code_indexer.server.services.ci_token_manager import CITokenManager
from src.code_indexer.server.services.git_credential_manager import GitCredentialManager

# Fake test token — not a real credential
TEST_GITHUB_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"

_FORGE_TYPE = "github"
_FORGE_HOST = "github.com"

_PBKDF2_ITERATIONS = 100000
_AES_KEY_SIZE = 32


def _derive_key(salt_input: str) -> bytes:
    """Replicate the PBKDF2 derivation used in both managers (for test assertions)."""
    salt = hashlib.sha256(salt_input.encode("utf-8")).digest()
    return hashlib.pbkdf2_hmac(
        "sha256",
        b"cidx-token-encryption-key",
        salt,
        _PBKDF2_ITERATIONS,
        dklen=_AES_KEY_SIZE,
    )


def _make_ci_db(path: Path) -> str:
    db_path = str(path / "cidx_server.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ci_tokens ("
            "  platform TEXT PRIMARY KEY,"
            "  encrypted_token TEXT NOT NULL,"
            "  base_url TEXT"
            ")"
        )
        conn.commit()
    return db_path


def _make_git_db(path: Path) -> str:
    db_path = str(path / "cidx_server.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_git_credentials (
                credential_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                forge_type TEXT NOT NULL,
                forge_host TEXT NOT NULL,
                encrypted_token TEXT NOT NULL,
                git_user_name TEXT,
                git_user_email TEXT,
                forge_username TEXT,
                name TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                UNIQUE(username, forge_type, forge_host)
            )"""
        )
        conn.commit()
    return db_path


def _insert_raw_git_credential(
    db_path: str, cred_id: str, username: str, encrypted_token: str
) -> None:
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_git_credentials "
            "(credential_id, username, forge_type, forge_host, encrypted_token, git_user_name, "
            "git_user_email, forge_username, name, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cred_id,
                username,
                _FORGE_TYPE,
                _FORGE_HOST,
                encrypted_token,
                None,
                None,
                None,
                None,
                now,
                None,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# CITokenManager — server_dir key derivation (Step 2)
# ---------------------------------------------------------------------------


class TestCITokenManagerServerDir:
    """Step 2: CITokenManager derives encryption key from .encryption_key_salt."""

    @pytest.fixture
    def server_dir(self, tmp_path):
        sd = tmp_path / ".cidx-server"
        sd.mkdir()
        (sd / "data").mkdir()
        return sd

    @pytest.fixture
    def db_path(self, server_dir):
        return _make_ci_db(server_dir / "data")

    def test_server_dir_with_salt_file_derives_key_from_salt(self, server_dir, db_path):
        """CITokenManager with server_dir reads .encryption_key_salt for key derivation."""
        salt_file = server_dir / ".encryption_key_salt"
        salt_file.write_text("my-custom-salt-value")

        mgr1 = CITokenManager(
            server_dir_path=str(server_dir),
            use_sqlite=True,
            db_path=str(db_path),
            server_dir=str(server_dir),
        )
        mgr1.save_token("github", TEST_GITHUB_TOKEN)

        mgr2 = CITokenManager(
            server_dir_path=str(server_dir),
            use_sqlite=True,
            db_path=str(db_path),
            server_dir=str(server_dir),
        )
        result = mgr2.get_token("github")
        assert result is not None
        assert result.token == TEST_GITHUB_TOKEN

    def test_different_salt_files_produce_different_keys(self, tmp_path):
        """Two CITokenManagers with different salt files derive different encryption keys."""
        sd1 = tmp_path / "sd1"
        sd1.mkdir()
        (sd1 / ".encryption_key_salt").write_text("salt-one")

        sd2 = tmp_path / "sd2"
        sd2.mkdir()
        (sd2 / ".encryption_key_salt").write_text("salt-two")

        mgr1 = CITokenManager(server_dir_path=str(sd1), server_dir=str(sd1))
        mgr2 = CITokenManager(server_dir_path=str(sd2), server_dir=str(sd2))

        assert mgr1._encryption_key != mgr2._encryption_key

    def test_try_decrypt_fallback_ci_token_legacy_hostname_key(
        self, server_dir, db_path
    ):
        """Step 3: Token encrypted with legacy hostname key is readable by salt-based manager.

        Scenario: token was stored before server_dir/salt feature (uses hostname key).
        Then server is upgraded, salt file created with different value.
        Manager should fall back to hostname key, decrypt successfully.
        """
        hostname = os.uname().nodename
        hostname_key = _derive_key(hostname)
        custom_salt = "completely-different-salt-" + uuid.uuid4().hex

        # Verify keys are actually different (test is only meaningful if they differ)
        custom_key = _derive_key(custom_salt)
        assert hostname_key != custom_key, "Test requires hostname key != custom key"

        # Step 1: Create ciphertext using legacy hostname-derived key (no server_dir)
        legacy_mgr = CITokenManager(
            server_dir_path=str(server_dir),
            use_sqlite=True,
            db_path=str(db_path),
        )
        legacy_mgr.save_token("github", TEST_GITHUB_TOKEN)

        # Step 2: Set up salt file with different value (simulating upgrade)
        salt_file = server_dir / ".encryption_key_salt"
        salt_file.write_text(custom_salt)

        # Step 3: New manager uses salt-based key, but falls back to hostname for old data
        new_mgr = CITokenManager(
            server_dir_path=str(server_dir),
            use_sqlite=True,
            db_path=str(db_path),
            server_dir=str(server_dir),
        )
        result = new_mgr.get_token("github")
        assert result is not None
        assert result.token == TEST_GITHUB_TOKEN


# ---------------------------------------------------------------------------
# GitCredentialManager — server_dir key derivation (Step 2)
# ---------------------------------------------------------------------------


class TestGitCredentialManagerServerDir:
    """Step 2: GitCredentialManager derives encryption key from .encryption_key_salt."""

    @pytest.fixture
    def server_dir(self, tmp_path):
        sd = tmp_path / ".cidx-server"
        sd.mkdir()
        return sd

    @pytest.fixture
    def db_path(self, server_dir):
        return _make_git_db(server_dir)

    def test_server_dir_with_salt_file_derives_key_from_salt(self, server_dir, db_path):
        """GitCredentialManager with server_dir reads .encryption_key_salt for key derivation."""
        salt_file = server_dir / ".encryption_key_salt"
        salt_file.write_text("my-custom-git-salt-value")

        mgr1 = GitCredentialManager(db_path=str(db_path), server_dir=str(server_dir))
        plaintext = uuid.uuid4().hex
        encrypted = mgr1._encrypt_token(plaintext)
        cred_id = uuid.uuid4().hex
        _insert_raw_git_credential(str(db_path), cred_id, "alice", encrypted)

        mgr2 = GitCredentialManager(db_path=str(db_path), server_dir=str(server_dir))
        result = mgr2.get_credential_for_host("alice", _FORGE_HOST)
        assert result is not None
        assert result["token"] == plaintext

    def test_different_salt_files_produce_different_keys(self, tmp_path):
        """Two GitCredentialManagers with different salt files derive different keys."""
        db_path = _make_git_db(tmp_path)

        sd1 = tmp_path / "sd1"
        sd1.mkdir()
        (sd1 / ".encryption_key_salt").write_text("salt-alpha")

        sd2 = tmp_path / "sd2"
        sd2.mkdir()
        (sd2 / ".encryption_key_salt").write_text("salt-beta")

        mgr1 = GitCredentialManager(db_path=str(db_path), server_dir=str(sd1))
        mgr2 = GitCredentialManager(db_path=str(db_path), server_dir=str(sd2))

        assert mgr1._encryption_key != mgr2._encryption_key

    def test_try_decrypt_fallback_git_credential_legacy_hostname_key(
        self, server_dir, db_path
    ):
        """Step 3: Credential encrypted with legacy hostname key is readable by salt-based manager.

        Scenario: credential stored before server_dir/salt feature (uses hostname key).
        Then server upgraded with a different salt. Manager falls back to hostname key.
        """
        hostname = os.uname().nodename
        hostname_key = _derive_key(hostname)
        custom_salt = "completely-different-salt-" + uuid.uuid4().hex
        custom_key = _derive_key(custom_salt)
        assert hostname_key != custom_key, "Test requires hostname key != custom key"

        # Step 1: Store credential via legacy manager (no server_dir → hostname key)
        legacy_mgr = GitCredentialManager(db_path=str(db_path))
        plaintext = uuid.uuid4().hex
        encrypted = legacy_mgr._encrypt_token(plaintext)
        cred_id = uuid.uuid4().hex
        _insert_raw_git_credential(str(db_path), cred_id, "bob", encrypted)

        # Step 2: Upgrade — create salt file with different value
        salt_file = server_dir / ".encryption_key_salt"
        salt_file.write_text(custom_salt)

        # Step 3: New manager with server_dir falls back to hostname for old data
        new_mgr = GitCredentialManager(db_path=str(db_path), server_dir=str(server_dir))
        result = new_mgr.get_credential_for_host("bob", _FORGE_HOST)
        assert result is not None
        assert result["token"] == plaintext
