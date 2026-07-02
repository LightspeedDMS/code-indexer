"""
Unit tests for create_token_manager factory function (Bug #639).

Tests that:
- Factory produces consistent CITokenManager instances
- Token survives simulated restart (same factory params = same encryption key)
- Decrypt failure does NOT delete the token from storage (preserves for recovery)
- Standalone mode does not use cluster_secret even when .jwt_secret exists
- Cluster mode uses .jwt_secret for shared encryption key
"""

import pytest

from src.code_indexer.server.services.ci_token_manager import (
    CITokenManager,
    create_token_manager,
)

# Fake test token — not a real credential
TEST_GITHUB_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"


class TestCreateTokenManagerFactory:
    """Tests for create_token_manager factory function (Bug #639)."""

    @pytest.fixture
    def temp_server_dir(self, tmp_path):
        """Create temporary server directory."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        data_dir = server_dir / "data"
        data_dir.mkdir()
        return server_dir

    @pytest.fixture
    def db_path(self, temp_server_dir):
        """Return path to SQLite database with ci_tokens table created."""
        import sqlite3

        path = str(temp_server_dir / "data" / "cidx_server.db")
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ci_tokens ("
                "  platform TEXT PRIMARY KEY,"
                "  encrypted_token TEXT NOT NULL,"
                "  base_url TEXT"
                ")"
            )
            conn.commit()
        return path

    def test_factory_returns_functional_token_manager(self, temp_server_dir, db_path):
        """Verify create_token_manager() returns a CITokenManager that can save and retrieve tokens."""
        # When creating via factory
        manager = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
        )

        # Then it is a CITokenManager
        assert isinstance(manager, CITokenManager)

        # And it can save and retrieve tokens
        manager.save_token("github", TEST_GITHUB_TOKEN)
        result = manager.get_token("github")

        assert result is not None
        assert result.token == TEST_GITHUB_TOKEN
        assert result.platform == "github"

    def test_token_survives_restart_same_key(self, temp_server_dir, db_path):
        """Save token with factory, create new factory instance with same params, verify token is readable."""
        # Given a token saved via the factory
        manager1 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
        )
        manager1.save_token("github", TEST_GITHUB_TOKEN)

        # When creating a NEW manager with same params (simulating restart)
        manager2 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
        )

        # Then the token is still readable
        result = manager2.get_token("github")
        assert result is not None
        assert result.token == TEST_GITHUB_TOKEN

    def test_get_token_does_not_delete_on_decrypt_failure(
        self, temp_server_dir, db_path
    ):
        """
        Save a token with one encryption key, attempt to read with a different key,
        verify it returns None but the token row is NOT deleted from the DB.
        """
        # Given .jwt_secret set to key1
        jwt_file = temp_server_dir / ".jwt_secret"
        jwt_file.write_text("secret-key-one")

        # And a token saved with that key
        manager_key1 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="postgres",
        )
        manager_key1.save_token("github", TEST_GITHUB_TOKEN)

        # Verify token was saved (use raw sqlite3 to avoid connection isolation)
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row_before = conn.execute(
                "SELECT * FROM ci_tokens WHERE platform = 'github'"
            ).fetchone()
        assert row_before is not None, "Token must be in DB before decrypt attempt"

        # When trying to decrypt with a DIFFERENT encryption key
        jwt_file.write_text("completely-different-secret-key")
        manager_key2 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="postgres",
        )

        # Then get_token returns None (can't decrypt)
        result = manager_key2.get_token("github")
        assert result is None

        # But the token row is STILL in the DB (not deleted)
        with sqlite3.connect(db_path) as conn:
            row_after = conn.execute(
                "SELECT * FROM ci_tokens WHERE platform = 'github'"
            ).fetchone()
        assert row_after is not None, (
            "Token must still exist in DB after decrypt failure — no destructive delete"
        )

    def test_create_token_manager_standalone_no_cluster_secret(
        self, temp_server_dir, db_path
    ):
        """
        Verify that in standalone mode (storage_mode="sqlite"), cluster_secret is NOT used
        even when .jwt_secret exists.

        Two managers created in sqlite mode must produce the same encryption key regardless
        of .jwt_secret file content.
        """
        # Given a .jwt_secret exists on disk
        jwt_file = temp_server_dir / ".jwt_secret"
        jwt_file.write_text("some-cluster-secret")

        # When creating a manager in standalone (sqlite) mode and saving a token
        manager1 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="sqlite",
        )
        manager1.save_token("github", TEST_GITHUB_TOKEN)

        # Simulate restart — change .jwt_secret (should NOT affect standalone mode)
        jwt_file.write_text("completely-different-secret")

        manager2 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="sqlite",
        )

        # Then token is still readable because standalone mode ignores .jwt_secret
        result = manager2.get_token("github")
        assert result is not None, (
            "Standalone mode must ignore .jwt_secret and use hostname-based key"
        )
        assert result.token == TEST_GITHUB_TOKEN

    def test_create_token_manager_cluster_uses_jwt_secret(
        self, temp_server_dir, db_path
    ):
        """
        Verify that in cluster mode (storage_mode="postgres"), the .jwt_secret IS used.

        Two managers with the SAME .jwt_secret content must produce the same encryption key,
        so tokens written by one are readable by the other.
        """
        # Given .jwt_secret is set
        jwt_file = temp_server_dir / ".jwt_secret"
        jwt_file.write_text("shared-cluster-secret-12345")

        # When manager1 saves a token
        manager1 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="postgres",
        )
        manager1.save_token("github", TEST_GITHUB_TOKEN)

        # And manager2 uses the same .jwt_secret (simulating another node or restart)
        manager2 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="postgres",
        )

        # Then manager2 can decrypt the token
        result = manager2.get_token("github")
        assert result is not None, (
            "Cluster mode: same .jwt_secret must produce same encryption key"
        )
        assert result.token == TEST_GITHUB_TOKEN
