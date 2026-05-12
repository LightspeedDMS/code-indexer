"""
Unit tests for GitCredentialManager cluster_secret support and create_git_credential_manager factory.

Tests cover:
- cluster_secret parameter changes key derivation in GitCredentialManager
- Standalone mode (no cluster_secret) uses hostname-based key (backward compatible)
- Cluster mode uses shared secret so tokens are readable across instances
- create_git_credential_manager factory reads .jwt_secret in postgres mode
- Factory does NOT use .jwt_secret in sqlite/standalone mode
- Factory handles missing .jwt_secret gracefully (falls back to hostname key)
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from src.code_indexer.server.services.git_credential_manager import (
    GitCredentialManager,
    create_git_credential_manager,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FORGE_TYPE = "github"
_FORGE_HOST = "github.com"


def _make_db(tmp_path: Path) -> str:
    """Create minimal SQLite DB with user_git_credentials table.

    Schema matches GitCredentialsSqliteBackend exactly: credential_id PRIMARY KEY,
    unique on (username, forge_type, forge_host), includes last_used_at column.
    """
    db_path = str(tmp_path / "cidx_server.db")
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


def _insert_raw_credential(
    db_path: str, cred_id: str, username: str, encrypted_token: str
) -> None:
    """Insert a credential directly into DB (bypassing manager encryption)."""
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
# Tests: GitCredentialManager.__init__ cluster_secret parameter
# ---------------------------------------------------------------------------


class TestGitCredentialManagerClusterSecret:
    """Tests for cluster_secret parameter in GitCredentialManager.__init__."""

    def test_standalone_uses_hostname_key(self, tmp_path):
        """Two standalone (no cluster_secret) managers on same machine derive same key."""
        db_path = _make_db(tmp_path)

        mgr1 = GitCredentialManager(db_path)
        mgr2 = GitCredentialManager(db_path)

        assert mgr1._encryption_key == mgr2._encryption_key

    def test_cluster_secret_derives_different_key(self, tmp_path):
        """A cluster_secret produces a different key than hostname-based derivation."""
        db_path = _make_db(tmp_path)
        cluster_secret = uuid.uuid4().hex

        mgr_standalone = GitCredentialManager(db_path)
        mgr_cluster = GitCredentialManager(db_path, cluster_secret=cluster_secret)

        assert mgr_standalone._encryption_key != mgr_cluster._encryption_key

    def test_cluster_mode_shared_key_readable_across_instances(self, tmp_path):
        """Token encrypted by cluster manager can be decrypted by another with same secret."""
        db_path = _make_db(tmp_path)
        secret = uuid.uuid4().hex
        cred_id = uuid.uuid4().hex
        username = uuid.uuid4().hex
        plaintext_token = uuid.uuid4().hex

        mgr1 = GitCredentialManager(db_path, cluster_secret=secret)
        encrypted = mgr1._encrypt_token(plaintext_token)
        _insert_raw_credential(db_path, cred_id, username, encrypted)

        mgr2 = GitCredentialManager(db_path, cluster_secret=secret)
        result = mgr2.get_credential_for_host(username, _FORGE_HOST)

        assert result is not None
        assert result["token"] == plaintext_token


# ---------------------------------------------------------------------------
# Tests: create_git_credential_manager factory
# ---------------------------------------------------------------------------


class TestCreateGitCredentialManagerFactory:
    """Tests for create_git_credential_manager factory function."""

    @pytest.fixture
    def server_dir(self, tmp_path):
        """Create a temp server directory."""
        sd = tmp_path / ".cidx-server"
        sd.mkdir()
        return sd

    @pytest.fixture
    def db_path(self, server_dir):
        """Create DB in server_dir."""
        return _make_db(server_dir)

    def test_create_git_credential_manager_standalone(self, server_dir, db_path):
        """In sqlite mode, .jwt_secret is ignored — changing file does not affect key."""
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text(uuid.uuid4().hex)

        mgr1 = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode="sqlite"
        )
        jwt_file.write_text(uuid.uuid4().hex)
        mgr2 = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode="sqlite"
        )

        # Both should use hostname-based key — identical despite jwt_secret change
        assert mgr1._encryption_key == mgr2._encryption_key

    def test_create_git_credential_manager_cluster_uses_jwt_secret(
        self, server_dir, db_path
    ):
        """In postgres mode, factory uses .jwt_secret — key matches direct cluster construction."""
        jwt_secret = uuid.uuid4().hex
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text(jwt_secret)

        # Factory-created cluster manager
        mgr_factory = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode="postgres"
        )

        # Directly-constructed cluster manager with same secret (ground truth)
        mgr_direct = GitCredentialManager(db_path, cluster_secret=jwt_secret)

        # Keys must match — proving factory reads .jwt_secret
        assert mgr_factory._encryption_key == mgr_direct._encryption_key

    def test_create_git_credential_manager_no_jwt_secret_in_cluster_mode(
        self, server_dir, db_path
    ):
        """In postgres mode without .jwt_secret, factory falls back to hostname key."""
        jwt_file = server_dir / ".jwt_secret"
        assert not jwt_file.exists()

        mgr_cluster = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode="postgres"
        )
        mgr_standalone = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode="sqlite"
        )

        # Both fall back to hostname key — must match
        assert mgr_cluster._encryption_key == mgr_standalone._encryption_key

    def test_create_git_credential_manager_invalid_storage_mode(
        self, server_dir, db_path
    ):
        """Factory raises ValueError for unsupported storage_mode."""
        with pytest.raises(ValueError, match="storage_mode"):
            create_git_credential_manager(
                db_path=db_path, server_dir=str(server_dir), storage_mode="invalid"
            )

    def test_create_git_credential_manager_empty_server_dir(self, db_path):
        """Factory raises ValueError when server_dir is empty."""
        with pytest.raises(ValueError, match="server_dir"):
            create_git_credential_manager(
                db_path=db_path, server_dir="", storage_mode="sqlite"
            )

    def test_create_git_credential_manager_empty_db_path(self, server_dir):
        """Factory raises ValueError when db_path is empty."""
        with pytest.raises(ValueError, match="db_path"):
            create_git_credential_manager(
                db_path="", server_dir=str(server_dir), storage_mode="sqlite"
            )
