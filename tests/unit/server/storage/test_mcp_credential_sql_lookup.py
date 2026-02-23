"""
Unit tests for UsersSqliteBackend.get_mcp_credential_by_client_id().

Story #269: Remove Unjustified SQLite Performance Indexes and Fix MCP Credential
Lookup Algorithm.

Tests written FIRST following TDD methodology (red phase).
All tests use real in-memory SQLite databases with zero mocking.
"""

import sqlite3
from pathlib import Path

import pytest


def _setup_db_with_users_and_credentials(db_path: Path) -> None:
    """
    Helper: Initialize database and insert 3 users, each with 2 MCP credentials.

    Users:
        - user1: cred1a (client_id='client_aaa'), cred1b (client_id='client_bbb')
        - user2: cred2a (client_id='client_ccc'), cred2b (client_id='client_ddd')
        - user3: cred3a (client_id='client_eee'), cred3b (client_id='client_fff')
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Insert 3 users
        for username in ["user1", "user2", "user3"]:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, 'hash', 'user', '2024-01-01T00:00:00Z')",
                (username,),
            )

        # Insert 2 credentials per user (6 total)
        credentials = [
            ("cred1a", "user1", "client_aaa", "hash_aaa", "mcp_a", "cred1a_name"),
            ("cred1b", "user1", "client_bbb", "hash_bbb", "mcp_b", "cred1b_name"),
            ("cred2a", "user2", "client_ccc", "hash_ccc", "mcp_c", "cred2a_name"),
            ("cred2b", "user2", "client_ddd", "hash_ddd", "mcp_d", "cred2b_name"),
            ("cred3a", "user3", "client_eee", "hash_eee", "mcp_e", "cred3a_name"),
            ("cred3b", "user3", "client_fff", "hash_fff", "mcp_f", "cred3b_name"),
        ]
        for cred_id, username, client_id, secret_hash, prefix, name in credentials:
            conn.execute(
                "INSERT INTO user_mcp_credentials "
                "(credential_id, username, client_id, client_secret_hash, "
                " client_id_prefix, name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '2024-01-01T00:00:00Z')",
                (cred_id, username, client_id, secret_hash, prefix, name),
            )
        conn.commit()
    finally:
        conn.close()


class TestGetMcpCredentialByClientId:
    """
    Tests for UsersSqliteBackend.get_mcp_credential_by_client_id().

    Acceptance criteria from Story #269, Scenario 4 and 5.
    """

    def test_returns_username_and_credential_dict_for_valid_client_id(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 4: MCP credential lookup uses direct SQL instead of Python iteration.

        Given a database with 3 users, each having 2 MCP credentials (6 total)
        When get_mcp_credential_by_client_id() is called with a valid client_id
            belonging to user2
        Then it returns (username, credential_dict) for the matching credential.
        """
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_lookup.db"
        _setup_db_with_users_and_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_mcp_credential_by_client_id("client_ccc")

        assert result is not None, "Expected a result for valid client_id 'client_ccc'"
        username, credential = result

        assert username == "user2", f"Expected username 'user2', got '{username}'"
        assert isinstance(credential, dict), "credential must be a dict"
        assert credential["client_id"] == "client_ccc"
        assert credential["credential_id"] == "cred2a"
        assert credential["client_secret_hash"] == "hash_ccc"
        assert credential["client_id_prefix"] == "mcp_c"
        assert credential["name"] == "cred2a_name"
        assert "created_at" in credential

        backend.close()

    def test_returns_none_for_unknown_client_id(self, tmp_path: Path) -> None:
        """
        Scenario 5: MCP credential lookup returns None for unknown client_id.

        Given a database with 3 users, each having 2 MCP credentials (6 total)
        When get_mcp_credential_by_client_id() is called with an unknown client_id
        Then it returns None.
        """
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_lookup_none.db"
        _setup_db_with_users_and_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_mcp_credential_by_client_id("client_zzz_nonexistent")

        assert result is None, (
            f"Expected None for unknown client_id, got: {result}"
        )

        backend.close()

    def test_returns_correct_credential_for_last_user(self, tmp_path: Path) -> None:
        """
        Verify correct credential is returned when target is not the first user.

        Given a database with 3 users having credentials
        When get_mcp_credential_by_client_id() is called for user3's credential
        Then it returns user3 and the correct credential dict.
        """
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_lookup_last.db"
        _setup_db_with_users_and_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_mcp_credential_by_client_id("client_fff")

        assert result is not None, "Expected a result for client_id 'client_fff'"
        username, credential = result

        assert username == "user3"
        assert credential["client_id"] == "client_fff"
        assert credential["credential_id"] == "cred3b"

        backend.close()

    def test_returns_correct_credential_for_second_credential_of_user(
        self, tmp_path: Path
    ) -> None:
        """
        Verify the second credential of a user is correctly returned.

        Given a database with user1 having two credentials (client_aaa, client_bbb)
        When get_mcp_credential_by_client_id() is called for client_bbb
        Then it returns user1 and credential cred1b (not cred1a).
        """
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_lookup_second.db"
        _setup_db_with_users_and_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_mcp_credential_by_client_id("client_bbb")

        assert result is not None, "Expected a result for client_id 'client_bbb'"
        username, credential = result

        assert username == "user1"
        assert credential["client_id"] == "client_bbb"
        assert credential["credential_id"] == "cred1b"

        backend.close()
