"""
Unit tests for encryption_key_salt module (Story #999).

Tests:
- ensure_encryption_key_salt in sqlite mode seeds from hostname (missing file case)
- ensure_encryption_key_salt in sqlite mode returns existing salt if already present
- ensure_encryption_key_salt in sqlite mode is idempotent on repeated calls
- ensure_encryption_key_salt in postgres mode reads .jwt_secret (stripped) and creates the salt file
- ensure_encryption_key_salt in postgres mode returns existing salt when file already exists
- ensure_encryption_key_salt in postgres mode falls back to hostname when .jwt_secret absent
- ensure_encryption_key_salt strips whitespace from .jwt_secret content (matching existing pattern)
- ensure_encryption_key_salt raises ValueError for unsupported storage_mode
- read_encryption_key_salt reads existing salt file content
- read_encryption_key_salt raises FileNotFoundError when file is missing
"""

import os

import pytest

from src.code_indexer.server.services.encryption_key_salt import (
    ensure_encryption_key_salt,
    read_encryption_key_salt,
)


class TestEnsureEncryptionKeySaltSqliteMode:
    """Tests for sqlite mode salt seeding."""

    def test_sqlite_mode_missing_file_seeds_from_hostname(self, tmp_path):
        """In sqlite mode, missing salt file is created with hostname content."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        result = ensure_encryption_key_salt(server_dir, "sqlite")

        assert result == os.uname().nodename
        salt_file = server_dir / ".encryption_key_salt"
        assert salt_file.exists()
        assert salt_file.read_text() == os.uname().nodename

    def test_sqlite_mode_returns_existing_salt_without_overwrite(self, tmp_path):
        """In sqlite mode, existing salt file is returned and not modified."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        salt_file = server_dir / ".encryption_key_salt"
        existing_salt = "my-existing-unique-salt-value"
        salt_file.write_text(existing_salt)

        result = ensure_encryption_key_salt(server_dir, "sqlite")

        assert result == existing_salt
        assert salt_file.read_text() == existing_salt

    def test_sqlite_mode_idempotent_on_repeated_calls(self, tmp_path):
        """Calling ensure twice in sqlite mode returns same value both times."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        result1 = ensure_encryption_key_salt(server_dir, "sqlite")
        result2 = ensure_encryption_key_salt(server_dir, "sqlite")

        assert result1 == result2

    def test_sqlite_mode_invalid_storage_mode_raises_value_error(self, tmp_path):
        """Unsupported storage_mode raises ValueError immediately."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        with pytest.raises(ValueError, match="storage_mode"):
            ensure_encryption_key_salt(server_dir, "unknown-mode")


class TestEnsureEncryptionKeySaltPostgresMode:
    """Tests for postgres mode salt seeding from .jwt_secret."""

    def test_postgres_mode_reads_jwt_secret_stripped_and_creates_salt_file(
        self, tmp_path
    ):
        """In postgres mode, .jwt_secret content (stripped) is written to .encryption_key_salt."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        jwt_file = server_dir / ".jwt_secret"
        jwt_secret_value = "shared-cluster-jwt-secret-abc123"
        jwt_file.write_text(jwt_secret_value)

        result = ensure_encryption_key_salt(server_dir, "postgres")

        assert result == jwt_secret_value
        salt_file = server_dir / ".encryption_key_salt"
        assert salt_file.exists()
        assert salt_file.read_text() == jwt_secret_value

    def test_postgres_mode_returns_existing_salt_when_file_already_present(
        self, tmp_path
    ):
        """In postgres mode, if .encryption_key_salt already exists, it is returned unchanged."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        salt_file = server_dir / ".encryption_key_salt"
        existing_salt = "pre-seeded-salt-value"
        salt_file.write_text(existing_salt)
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text("different-jwt-secret")

        result = ensure_encryption_key_salt(server_dir, "postgres")

        assert result == existing_salt
        assert salt_file.read_text() == existing_salt

    def test_postgres_mode_no_jwt_secret_falls_back_to_hostname(self, tmp_path):
        """In postgres mode with no .jwt_secret, falls back to hostname."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        result = ensure_encryption_key_salt(server_dir, "postgres")

        assert result == os.uname().nodename
        salt_file = server_dir / ".encryption_key_salt"
        assert salt_file.exists()
        assert salt_file.read_text() == os.uname().nodename

    def test_postgres_mode_strips_whitespace_from_jwt_secret(self, tmp_path):
        """Whitespace around .jwt_secret content is stripped (matching create_git_credential_manager pattern)."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        jwt_file = server_dir / ".jwt_secret"
        jwt_file.write_text("  my-secret-with-spaces  \n")

        result = ensure_encryption_key_salt(server_dir, "postgres")

        assert result == "my-secret-with-spaces"
        salt_file = server_dir / ".encryption_key_salt"
        assert salt_file.read_text() == "my-secret-with-spaces"


class TestEncryptionKeySaltFilePermissions:
    """Tests that .encryption_key_salt is created with restricted permissions (Finding 3)."""

    def test_created_salt_file_has_0600_permissions(self, tmp_path):
        """ensure_encryption_key_salt creates the file with mode 0600 (owner read/write only)."""
        import stat

        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        ensure_encryption_key_salt(server_dir, "sqlite")

        salt_file = server_dir / ".encryption_key_salt"
        assert salt_file.exists()
        file_mode = stat.S_IMODE(os.stat(salt_file).st_mode)
        assert file_mode == 0o600, (
            f"Expected 0o600 permissions on .encryption_key_salt, got 0o{file_mode:o}"
        )


class TestReadEncryptionKeySalt:
    """Tests for read_encryption_key_salt helper."""

    def test_reads_existing_salt_file_content(self, tmp_path):
        """read_encryption_key_salt returns content of existing .encryption_key_salt."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()
        salt_file = server_dir / ".encryption_key_salt"
        salt_file.write_text("my-salt-content")

        result = read_encryption_key_salt(server_dir)

        assert result == "my-salt-content"

    def test_raises_file_not_found_when_salt_file_missing(self, tmp_path):
        """read_encryption_key_salt raises FileNotFoundError if file does not exist."""
        server_dir = tmp_path / ".cidx-server"
        server_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            read_encryption_key_salt(server_dir)
