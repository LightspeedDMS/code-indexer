"""
Unit tests for create_token_manager factory function (Bug #639, Story #999).

Tests that:
- Factory produces consistent CITokenManager instances
- Token survives simulated restart (same factory params = same encryption key)
- Decrypt failure does NOT delete the token from storage (preserves for recovery)
- Standalone mode does not use .jwt_secret
- Cluster mode uses .encryption_key_salt (seeded from .jwt_secret) for shared key
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

        Story #999: Key mismatch simulated by writing .encryption_key_salt directly
        with initial value for manager_key1, then overwriting with different value
        for manager_key2. No jwt_secret dependency in this test.
        """
        import sqlite3

        # Write .encryption_key_salt with initial value so manager_key1 uses it
        salt_file = temp_server_dir / ".encryption_key_salt"
        salt_file.write_text("initial-salt-value-one")

        manager_key1 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="postgres",
        )
        manager_key1.save_token("github", TEST_GITHUB_TOKEN)

        # Verify token was saved
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row_before = conn.execute(
                "SELECT * FROM ci_tokens WHERE platform = 'github'"
            ).fetchone()
        assert row_before is not None, "Token must be in DB before decrypt attempt"

        # Overwrite salt file with a completely different value to force key mismatch
        salt_file.write_text("completely-different-salt-value")

        manager_key2 = create_token_manager(
            server_dir=str(temp_server_dir),
            db_path=db_path,
            storage_mode="postgres",
        )

        # Then get_token returns None (can't decrypt with different key)
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


# ---------------------------------------------------------------------------
# Test constants — obviously synthetic, not real credentials
# ---------------------------------------------------------------------------

_CI_TEST_SALT = "test-ci-salt-not-a-real-secret"
_CI_TEST_TOKEN = "not-a-real-ci-token-test-only-0001"


# ---------------------------------------------------------------------------
# Helper for DB reads
# ---------------------------------------------------------------------------


def _read_ci_stored_enc(db_path: str, platform: str) -> str:
    """Return encrypted_token stored in ci_tokens table for platform."""
    import sqlite3 as _sq3

    with _sq3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT encrypted_token FROM ci_tokens WHERE platform = ?",
            (platform,),
        ).fetchone()
    # Explicit str() cast satisfies mypy no-any-return for sqlite3 column values.
    return str(row[0])


def _insert_ci_raw_token(db_path: str, platform: str, encrypted_token: str) -> None:
    """Insert a ci_token row directly (bypassing manager encryption)."""
    import sqlite3 as _sq3

    with _sq3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ci_tokens (platform, encrypted_token, base_url) VALUES (?, ?, ?)",
            (platform, encrypted_token, None),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Tests: Lazy re-encryption on fallback hit in CITokenManager
# ---------------------------------------------------------------------------


class TestCITokenLazyReencryption:
    """Tests verifying get_token triggers re-encryption in the DB on fallback key hit."""

    @pytest.fixture
    def ci_server_dir(self, tmp_path):
        """Server dir with .encryption_key_salt set to _CI_TEST_SALT."""
        sd = tmp_path / ".cidx-server"
        sd.mkdir()
        (sd / ".encryption_key_salt").write_text(_CI_TEST_SALT)
        return sd

    @pytest.fixture
    def ci_db_path(self, tmp_path):
        """SQLite DB with ci_tokens table."""
        import sqlite3 as _sq3

        path = str(tmp_path / "cidx_server.db")
        with _sq3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ci_tokens ("
                "  platform TEXT PRIMARY KEY,"
                "  encrypted_token TEXT NOT NULL,"
                "  base_url TEXT"
                ")"
            )
            conn.commit()
        return path

    @pytest.fixture
    def seeded_ci_credential(self, ci_db_path, ci_server_dir):
        """Insert a ci_token encrypted with hostname (old) key.

        Returns (db_path, canonical_mgr, platform, original_enc).
        """
        platform = "github"
        hostname_mgr = CITokenManager(
            server_dir_path=str(ci_server_dir.parent),
            use_sqlite=True,
            db_path=ci_db_path,
        )  # no server_dir= -> uses hostname key
        original_enc = hostname_mgr._encrypt_token(_CI_TEST_TOKEN)
        _insert_ci_raw_token(ci_db_path, platform, original_enc)
        canonical_mgr = CITokenManager(
            server_dir_path=str(ci_server_dir.parent),
            use_sqlite=True,
            db_path=ci_db_path,
            server_dir=str(ci_server_dir),
        )
        return ci_db_path, canonical_mgr, platform, original_enc

    def test_fallback_hit_get_token_reencrypts_and_second_call_canonical(
        self, seeded_ci_credential, caplog
    ):
        """get_token: fallback decrypt -> re-encrypts -> second call no fallback warning."""
        import logging

        db_path, canonical_mgr, platform, original_enc = seeded_ci_credential

        # First call: triggers fallback and re-encryption
        result = canonical_mgr.get_token(platform)
        assert result is not None
        assert result.token == _CI_TEST_TOKEN

        new_enc = _read_ci_stored_enc(db_path, platform)
        assert new_enc != original_enc
        assert (
            canonical_mgr._do_decrypt(new_enc, canonical_mgr._encryption_key)
            == _CI_TEST_TOKEN
        )

        # Second call: must not trigger fallback warning
        caplog.clear()
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.ci_token_manager",
        ):
            result2 = canonical_mgr.get_token(platform)
        assert result2 is not None
        assert result2.token == _CI_TEST_TOKEN
        fallback_warnings = [
            r for r in caplog.records if "fallback" in r.message.lower()
        ]
        assert fallback_warnings == [], "Second call must not trigger fallback warning"

    def test_no_reencryption_when_canonical_key_succeeds(
        self, ci_db_path, ci_server_dir
    ):
        """When canonical key decrypts successfully, DB token is unchanged."""
        mgr = CITokenManager(
            server_dir_path=str(ci_server_dir.parent),
            use_sqlite=True,
            db_path=ci_db_path,
            server_dir=str(ci_server_dir),
        )
        enc = mgr._encrypt_token(_CI_TEST_TOKEN)
        _insert_ci_raw_token(ci_db_path, "gitlab", enc)

        result = mgr.get_token("gitlab")

        assert result is not None
        assert result.token == _CI_TEST_TOKEN
        assert _read_ci_stored_enc(ci_db_path, "gitlab") == enc  # unchanged
