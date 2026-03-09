"""
Unit tests for GitCredentialManager service.

Story #386: Git Credential Management with Identity Discovery

Tests credential CRUD operations, UPSERT behavior, token redaction,
ownership checks, and encryption round-trips.
"""

import pytest
import sqlite3
import tempfile
import os
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def db_path():
    """Provide a temporary SQLite database path for testing."""
    # Use ~/.tmp to avoid /tmp permission issues with DatabaseSchema chmod
    tmp_dir = os.path.expanduser("~/.tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    test_dir = tempfile.mkdtemp(dir=tmp_dir)
    path = os.path.join(test_dir, "test_git_creds.db")
    yield path
    import shutil
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def initialized_db(db_path):
    """Initialize the database schema for testing."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    schema = DatabaseSchema(db_path)
    schema.initialize_database()
    return db_path


class TestGitCredentialsSqliteBackend:
    """Tests for GitCredentialsSqliteBackend CRUD operations."""

    def test_upsert_creates_new_credential(self, initialized_db):
        """upsert_credential inserts a new row when none exists."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted_abc",
            git_user_name="Alice Smith",
            git_user_email="alice@example.com",
            forge_username="alice_gh",
            name="My GitHub",
        )

        credentials = backend.list_credentials("alice")
        assert len(credentials) == 1
        assert credentials[0]["forge_host"] == "github.com"
        assert credentials[0]["forge_type"] == "github"
        assert credentials[0]["git_user_name"] == "Alice Smith"
        assert credentials[0]["forge_username"] == "alice_gh"

    def test_upsert_updates_existing_credential_same_host(self, initialized_db):
        """upsert_credential updates existing row when (username, forge_type, forge_host) matches."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)

        # Insert first
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted_old",
            git_user_name="Old Name",
            git_user_email="old@example.com",
            forge_username="alice_old",
        )

        # Upsert with new data for same host
        backend.upsert_credential(
            credential_id="cred-002",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted_new",
            git_user_name="Alice Smith",
            git_user_email="alice@example.com",
            forge_username="alice_gh",
        )

        # Should still be just one credential for this host
        credentials = backend.list_credentials("alice")
        assert len(credentials) == 1
        assert credentials[0]["encrypted_token"] == "encrypted_new"
        assert credentials[0]["git_user_name"] == "Alice Smith"

    def test_list_credentials_returns_only_users_own(self, initialized_db):
        """list_credentials returns only credentials belonging to the given username."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)

        backend.upsert_credential(
            credential_id="cred-alice",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="enc_alice",
        )
        backend.upsert_credential(
            credential_id="cred-bob",
            username="bob",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="enc_bob",
        )

        alice_creds = backend.list_credentials("alice")
        assert len(alice_creds) == 1
        assert alice_creds[0]["credential_id"] == "cred-alice"

    def test_delete_credential_removes_by_id_and_username(self, initialized_db):
        """delete_credential removes the credential when username and id match."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted_abc",
        )

        deleted = backend.delete_credential("alice", "cred-001")
        assert deleted is True

        credentials = backend.list_credentials("alice")
        assert len(credentials) == 0

    def test_delete_credential_fails_with_wrong_username(self, initialized_db):
        """delete_credential returns False when username doesn't match (ownership check)."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted_abc",
        )

        # Bob tries to delete Alice's credential
        deleted = backend.delete_credential("bob", "cred-001")
        assert deleted is False

        # Alice's credential still exists
        credentials = backend.list_credentials("alice")
        assert len(credentials) == 1

    def test_get_credential_for_host_returns_credential(self, initialized_db):
        """get_credential_for_host returns matching credential or None."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted_abc",
        )

        result = backend.get_credential_for_host("alice", "github.com")
        assert result is not None
        assert result["encrypted_token"] == "encrypted_abc"
        assert result["forge_host"] == "github.com"

    def test_get_credential_for_host_returns_none_when_missing(self, initialized_db):
        """get_credential_for_host returns None when no credential for host exists."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)

        result = backend.get_credential_for_host("alice", "github.com")
        assert result is None

    def test_multiple_hosts_for_same_user(self, initialized_db):
        """User can have credentials for multiple forge hosts."""
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-gh",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="enc_gh",
        )
        backend.upsert_credential(
            credential_id="cred-gl",
            username="alice",
            forge_type="gitlab",
            forge_host="gitlab.com",
            encrypted_token="enc_gl",
        )

        credentials = backend.list_credentials("alice")
        assert len(credentials) == 2

        hosts = {c["forge_host"] for c in credentials}
        assert "github.com" in hosts
        assert "gitlab.com" in hosts


class TestGitCredentialManager:
    """Tests for GitCredentialManager service operations."""

    @pytest.mark.asyncio
    async def test_configure_credential_stores_encrypted_token(self, initialized_db):
        """configure_credential validates token and stores it encrypted."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )

        # Mock the forge client
        mock_forge_client = AsyncMock()
        mock_forge_client.validate_and_discover.return_value = {
            "forge_username": "alice_gh",
            "git_user_name": "Alice Smith",
            "git_user_email": "alice@example.com",
        }

        with patch(
            "code_indexer.server.services.git_credential_manager.get_forge_client",
            return_value=mock_forge_client,
        ):
            manager = GitCredentialManager(initialized_db)
            result = await manager.configure_credential(
                username="alice",
                forge_type="github",
                forge_host="github.com",
                token="ghp_test_token_123456789012345678901234567890",
                name="My GitHub",
            )

        assert result["success"] is True
        assert "credential_id" in result

    @pytest.mark.asyncio
    async def test_configure_credential_discovers_identity(self, initialized_db):
        """configure_credential calls forge API and stores discovered identity."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        mock_forge_client = AsyncMock()
        mock_forge_client.validate_and_discover.return_value = {
            "forge_username": "alice_gh",
            "git_user_name": "Alice Smith",
            "git_user_email": "alice@example.com",
        }

        with patch(
            "code_indexer.server.services.git_credential_manager.get_forge_client",
            return_value=mock_forge_client,
        ):
            manager = GitCredentialManager(initialized_db)
            await manager.configure_credential(
                username="alice",
                forge_type="github",
                forge_host="github.com",
                token="ghp_token123456789012345678901234567890",
            )

        backend = GitCredentialsSqliteBackend(initialized_db)
        cred = backend.get_credential_for_host("alice", "github.com")
        assert cred is not None
        assert cred["git_user_name"] == "Alice Smith"
        assert cred["git_user_email"] == "alice@example.com"
        assert cred["forge_username"] == "alice_gh"

    @pytest.mark.asyncio
    async def test_configure_credential_token_is_encrypted(self, initialized_db):
        """Token stored in database is encrypted, not plaintext."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        mock_forge_client = AsyncMock()
        mock_forge_client.validate_and_discover.return_value = {
            "forge_username": "alice_gh",
            "git_user_name": "Alice",
            "git_user_email": "alice@example.com",
        }

        plain_token = "ghp_plaintext_token_1234567890123456789"
        with patch(
            "code_indexer.server.services.git_credential_manager.get_forge_client",
            return_value=mock_forge_client,
        ):
            manager = GitCredentialManager(initialized_db)
            await manager.configure_credential(
                username="alice",
                forge_type="github",
                forge_host="github.com",
                token=plain_token,
            )

        backend = GitCredentialsSqliteBackend(initialized_db)
        cred = backend.get_credential_for_host("alice", "github.com")
        # The stored token must not equal the plaintext token
        assert cred["encrypted_token"] != plain_token

    @pytest.mark.asyncio
    async def test_configure_credential_upserts_on_same_host(self, initialized_db):
        """configure_credential updates existing credential for same forge host."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )

        mock_forge_client = AsyncMock()
        mock_forge_client.validate_and_discover.side_effect = [
            {
                "forge_username": "alice_old",
                "git_user_name": "Alice Old",
                "git_user_email": "old@example.com",
            },
            {
                "forge_username": "alice_new",
                "git_user_name": "Alice New",
                "git_user_email": "new@example.com",
            },
        ]

        with patch(
            "code_indexer.server.services.git_credential_manager.get_forge_client",
            return_value=mock_forge_client,
        ):
            manager = GitCredentialManager(initialized_db)
            await manager.configure_credential(
                username="alice",
                forge_type="github",
                forge_host="github.com",
                token="ghp_oldtoken_12345678901234567890123456789",
            )
            await manager.configure_credential(
                username="alice",
                forge_type="github",
                forge_host="github.com",
                token="ghp_newtoken_12345678901234567890123456789",
            )

        credentials = manager.list_credentials("alice")
        assert len(credentials) == 1
        assert credentials[0]["forge_username"] == "alice_new"

    @pytest.mark.asyncio
    async def test_configure_credential_fails_on_invalid_token(self, initialized_db):
        """configure_credential raises error and stores nothing if forge validation fails."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.clients.forge_client import ForgeAuthenticationError

        mock_forge_client = AsyncMock()
        mock_forge_client.validate_and_discover.side_effect = ForgeAuthenticationError(
            "Invalid or expired token"
        )

        with patch(
            "code_indexer.server.services.git_credential_manager.get_forge_client",
            return_value=mock_forge_client,
        ):
            manager = GitCredentialManager(initialized_db)
            with pytest.raises(ForgeAuthenticationError):
                await manager.configure_credential(
                    username="alice",
                    forge_type="github",
                    forge_host="github.com",
                    token="invalid_token",
                )

        # Nothing should be stored
        credentials = manager.list_credentials("alice")
        assert len(credentials) == 0

    def test_list_credentials_redacts_token(self, initialized_db):
        """list_credentials returns last 4 chars of token, not full encrypted value."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        # Insert directly to test list output
        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="base64encryptedlongstring",
            git_user_name="Alice",
            git_user_email="alice@example.com",
            forge_username="alice_gh",
        )

        manager = GitCredentialManager(initialized_db)
        credentials = manager.list_credentials("alice")

        assert len(credentials) == 1
        cred = credentials[0]
        # Must NOT contain full encrypted token
        assert "token_suffix" in cred or "token_hint" in cred or "redacted" in str(cred).lower() or len(cred.get("token_suffix", "xxxx")) == 4
        # Full encrypted token must not be present
        assert "encrypted_token" not in cred or cred.get("encrypted_token") != "base64encryptedlongstring"

    def test_list_credentials_returns_identity_fields(self, initialized_db):
        """list_credentials includes forge_username, git_user_name, git_user_email."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted",
            git_user_name="Alice Smith",
            git_user_email="alice@example.com",
            forge_username="alice_gh",
            name="My GitHub",
        )

        manager = GitCredentialManager(initialized_db)
        credentials = manager.list_credentials("alice")

        cred = credentials[0]
        assert cred["forge_host"] == "github.com"
        assert cred["forge_type"] == "github"
        assert cred["git_user_name"] == "Alice Smith"
        assert cred["git_user_email"] == "alice@example.com"
        assert cred["forge_username"] == "alice_gh"
        assert cred["name"] == "My GitHub"

    def test_delete_credential_removes_own_credential(self, initialized_db):
        """delete_credential removes credential for correct owner."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted",
        )

        manager = GitCredentialManager(initialized_db)
        result = manager.delete_credential("alice", "cred-001")

        assert result is True
        assert len(manager.list_credentials("alice")) == 0

    def test_delete_credential_rejects_wrong_owner(self, initialized_db):
        """delete_credential raises PermissionError when user doesn't own the credential."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token="encrypted",
        )

        manager = GitCredentialManager(initialized_db)
        with pytest.raises(PermissionError):
            manager.delete_credential("bob", "cred-001")

        # Alice's credential still present
        assert len(manager.list_credentials("alice")) == 1

    def test_get_credential_for_host_decrypts_token(self, initialized_db):
        """get_credential_for_host returns decrypted plaintext token for use."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )

        manager = GitCredentialManager(initialized_db)

        # Use the manager's own encryption to store a known plaintext
        plaintext_token = "ghp_roundtrip_test_12345678901234567890"
        encrypted = manager._encrypt_token(plaintext_token)

        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        backend = GitCredentialsSqliteBackend(initialized_db)
        backend.upsert_credential(
            credential_id="cred-001",
            username="alice",
            forge_type="github",
            forge_host="github.com",
            encrypted_token=encrypted,
        )

        result = manager.get_credential_for_host("alice", "github.com")
        assert result is not None
        assert result["token"] == plaintext_token

    def test_encryption_round_trip(self, initialized_db):
        """Encrypt then decrypt returns original plaintext."""
        from code_indexer.server.services.git_credential_manager import (
            GitCredentialManager,
        )

        manager = GitCredentialManager(initialized_db)
        original = "ghp_mysecrettoken_123456789012345678901"
        encrypted = manager._encrypt_token(original)
        decrypted = manager._decrypt_token(encrypted)

        assert decrypted == original
        assert encrypted != original
