"""
Tests for factory functions using ensure_encryption_key_salt (Story #999 Step 7).

Verifies that create_token_manager and create_git_credential_manager factories:
- In postgres mode, seed .encryption_key_salt from .jwt_secret so the derived
  encryption key is consistent across factory instances.
- In postgres mode, create the .encryption_key_salt file from .jwt_secret.
- Two postgres factory instances with the same .jwt_secret derive the same key.

For the CI token roundtrip test, we bypass save_token() format validation by
inserting an encrypted value directly into SQLite (CITokenManager._encrypt_token()
is called directly to produce ciphertext, then inserted; get_token() decrypts it).
This approach avoids any real-format token string in the test file.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from src.code_indexer.server.services.ci_token_manager import create_token_manager
from src.code_indexer.server.services.git_credential_manager import (
    create_git_credential_manager,
)

# Clearly fake placeholder — does NOT match any real token format
FAKE_PLAINTEXT = "plaintext-value-for-encryption-test-only"


def _make_ci_db(path: Path) -> str:
    db_path = str(path)
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
    db_path = str(path)
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


def _insert_ci_token(db_path: str, platform: str, encrypted_token: str) -> None:
    """Insert a raw encrypted token directly into ci_tokens (bypasses format validation)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ci_tokens (platform, encrypted_token, base_url) "
            "VALUES (?, ?, NULL)",
            (platform, encrypted_token),
        )
        conn.commit()


class TestCreateTokenManagerSaltIntegration:
    """Step 7: create_token_manager seeds .encryption_key_salt and uses it for key derivation."""

    @pytest.fixture
    def server_dir(self, tmp_path):
        sd = tmp_path / ".cidx-server"
        sd.mkdir()
        (sd / "data").mkdir()
        return sd

    @pytest.fixture
    def db_path(self, server_dir):
        return _make_ci_db(server_dir / "data" / "cidx_server.db")

    def test_postgres_factory_two_instances_derive_same_key_from_salt(
        self, server_dir, db_path
    ):
        """Two create_token_manager postgres instances with same .jwt_secret derive same key."""
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text(uuid.uuid4().hex)

        mgr1 = create_token_manager(
            server_dir=str(server_dir),
            db_path=str(db_path),
            storage_mode="postgres",
        )
        mgr2 = create_token_manager(
            server_dir=str(server_dir),
            db_path=str(db_path),
            storage_mode="postgres",
        )

        assert mgr1._encryption_key == mgr2._encryption_key

    def test_postgres_factory_salt_file_created_from_jwt_secret(
        self, server_dir, db_path
    ):
        """create_token_manager in postgres mode creates .encryption_key_salt from .jwt_secret."""
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text("my-cluster-secret")
        salt_file = server_dir / ".encryption_key_salt"
        assert not salt_file.exists()

        create_token_manager(
            server_dir=str(server_dir),
            db_path=str(db_path),
            storage_mode="postgres",
        )

        assert salt_file.exists()
        assert salt_file.read_text() == "my-cluster-secret"

    def test_postgres_factory_roundtrip_via_direct_insert(self, server_dir, db_path):
        """Roundtrip: encrypt via factory1, insert directly, read back via factory2.

        Bypasses save_token() format validation by inserting ciphertext directly.
        Verifies that two factory instances with the same salt can share encrypted data.
        """
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text(uuid.uuid4().hex)

        mgr1 = create_token_manager(
            server_dir=str(server_dir),
            db_path=str(db_path),
            storage_mode="postgres",
        )
        encrypted = mgr1._encrypt_token(FAKE_PLAINTEXT)
        _insert_ci_token(str(db_path), "github", encrypted)

        mgr2 = create_token_manager(
            server_dir=str(server_dir),
            db_path=str(db_path),
            storage_mode="postgres",
        )
        result = mgr2.get_token("github")
        assert result is not None
        assert result.token == FAKE_PLAINTEXT


class TestCreateGitCredentialManagerSaltIntegration:
    """Step 7: create_git_credential_manager seeds .encryption_key_salt and uses it."""

    @pytest.fixture
    def server_dir(self, tmp_path):
        sd = tmp_path / ".cidx-server"
        sd.mkdir()
        return sd

    @pytest.fixture
    def db_path(self, server_dir):
        return _make_git_db(server_dir / "cidx_server.db")

    def test_postgres_factory_creates_salt_file_from_jwt_secret(
        self, server_dir, db_path
    ):
        """create_git_credential_manager in postgres mode creates .encryption_key_salt from .jwt_secret."""
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text("my-git-cluster-secret")
        salt_file = server_dir / ".encryption_key_salt"
        assert not salt_file.exists()

        create_git_credential_manager(
            db_path=str(db_path),
            server_dir=str(server_dir),
            storage_mode="postgres",
        )

        assert salt_file.exists()
        assert salt_file.read_text() == "my-git-cluster-secret"

    def test_postgres_factory_two_instances_derive_same_key(self, server_dir, db_path):
        """Two create_git_credential_manager factories with same .jwt_secret produce same key."""
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text("shared-git-secret")

        mgr1 = create_git_credential_manager(
            db_path=str(db_path),
            server_dir=str(server_dir),
            storage_mode="postgres",
        )
        mgr2 = create_git_credential_manager(
            db_path=str(db_path),
            server_dir=str(server_dir),
            storage_mode="postgres",
        )

        assert mgr1._encryption_key == mgr2._encryption_key
