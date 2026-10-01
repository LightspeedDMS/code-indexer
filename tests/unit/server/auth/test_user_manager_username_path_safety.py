"""
Tests for UserManager username path-safety validation.

UserManager.create_user / create_oidc_user is the single place every
account-creation path ultimately funnels through (self-registration, admin
REST create, web-UI create, MCP create_user, OIDC JIT provisioning) -- so
this is where the centralized username gate must live, independent
of any Pydantic-level validation a specific front door happens to apply.

Foundation #1 compliant: real UserManager against a real SQLite backend
and a real JSON-file backend in a temp directory -- no mocks.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.auth.user_manager import UserManager, UserRole


class TestCreateUserRejectsUnsafeUsernamesSqliteBackend:
    """Real SQLite-backed UserManager -- the traversal username
    ('..') must raise and create no row."""

    @pytest.fixture
    def manager(self):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "users.db")
            DatabaseSchema(db_path).initialize_database()
            yield UserManager(use_sqlite=True, db_path=db_path), db_path

    def test_create_user_dotdot_raises_and_creates_no_row(self, manager):
        user_manager, db_path = manager

        with pytest.raises(ValueError):
            user_manager.create_user("..", "SecurePass123!@#", UserRole.NORMAL_USER)

        assert user_manager.get_user("..") is None
        # Verify directly against the SQLite file -- no row at all, not even
        # a partially-written one.
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = ?", ("..",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_create_user_single_dot_raises(self, manager):
        user_manager, _ = manager
        with pytest.raises(ValueError):
            user_manager.create_user(".", "SecurePass123!@#", UserRole.NORMAL_USER)

    def test_create_user_slash_raises(self, manager):
        user_manager, _ = manager
        with pytest.raises(ValueError):
            user_manager.create_user("a/b", "SecurePass123!@#", UserRole.NORMAL_USER)

    def test_create_user_backslash_raises(self, manager):
        user_manager, _ = manager
        with pytest.raises(ValueError):
            user_manager.create_user("a\\b", "SecurePass123!@#", UserRole.NORMAL_USER)

    def test_create_user_legitimate_username_still_works(self, manager):
        user_manager, _ = manager
        user = user_manager.create_user(
            "alice", "SecurePass123!@#", UserRole.NORMAL_USER
        )
        assert user.username == "alice"
        assert user_manager.get_user("alice") is not None


class TestCreateUserRejectsUnsafeUsernamesJsonBackend:
    """Legacy JSON-file backend gets the same gate."""

    @pytest.fixture
    def manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = str(Path(tmpdir) / "users.json")
            yield UserManager(users_file_path=users_file)

    def test_create_user_dotdot_raises_and_creates_no_row(self, manager):
        with pytest.raises(ValueError):
            manager.create_user("..", "SecurePass123!@#", UserRole.NORMAL_USER)
        assert manager.get_user("..") is None

    def test_create_user_legitimate_username_still_works(self, manager):
        user = manager.create_user(
            "bob.smith", "SecurePass123!@#", UserRole.NORMAL_USER
        )
        assert user.username == "bob.smith"


class TestCreateOidcUserRejectsUnsafeUsernames:
    """JIT OIDC provisioning must go through the same gate -- an IdP-derived
    username is input this server does not control (the IdP decides its
    value, and a username_claim may point at a free-text field)."""

    @pytest.fixture
    def manager(self):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "users.db")
            DatabaseSchema(db_path).initialize_database()
            yield UserManager(use_sqlite=True, db_path=db_path)

    def test_create_oidc_user_dotdot_raises_and_creates_no_row(self, manager):
        with pytest.raises(ValueError):
            manager.create_oidc_user(
                username="..",
                role=UserRole.NORMAL_USER,
                email="attacker@example.com",
                oidc_identity={"subject": "sub-123"},
            )
        assert manager.get_user("..") is None

    def test_create_oidc_user_legitimate_upn_username_still_works(self, manager):
        user = manager.create_oidc_user(
            username="jane.doe@example.com",
            role=UserRole.NORMAL_USER,
            email="jane.doe@example.com",
            oidc_identity={"subject": "sub-456"},
        )
        assert user.username == "jane.doe@example.com"
        assert manager.get_user("jane.doe@example.com") is not None
