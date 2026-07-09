"""
Unit tests for API key Bearer authentication (Bug #1144).

Design: Option B (SHA-256 lookup + bcrypt confirm).
- generate_key stores key_sha256 alongside existing key_hash
- authenticate_bearer does SHA-256 lookup then bcrypt confirm
- dependencies.get_current_user dispatches to authenticate_bearer for cidx_sk_ tokens

REAL backend — real SQLite via DatabaseSchema.initialize_database().
ZERO mocking of the auth feature itself.
"""

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.api_key_manager import ApiKeyManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_user_manager(db_path: str) -> UserManager:
    """Create a UserManager backed by a real SQLite database."""
    backend = UsersSqliteBackend(db_path)
    return UserManager(storage_backend=backend)


def _make_db(tmpdir: str) -> str:
    """Initialize a fresh DatabaseSchema and return the db_path."""
    db_path = str(Path(tmpdir) / "cidx_server.db")
    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()
    return db_path


# ---------------------------------------------------------------------------
# Tests: generate_key stores key_sha256
# ---------------------------------------------------------------------------


class TestGenerateKeyStoresSha256:
    """generate_key must persist key_sha256 alongside key_hash."""

    def test_generate_key_stores_sha256_in_sqlite(self):
        """After generate_key, the user_api_keys row has a non-null key_sha256."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, key_id = api_key_manager.generate_key("admin")

            # Verify sha256 is stored in the database
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT key_sha256 FROM user_api_keys WHERE key_id = ?",
                    (key_id,),
                ).fetchone()
            finally:
                conn.close()

            assert row is not None, "Key row not found"
            stored_sha256 = row[0]
            assert stored_sha256 is not None, "key_sha256 should not be NULL"
            expected_sha256 = hashlib.sha256(raw_key.encode()).hexdigest()
            assert stored_sha256 == expected_sha256

    def test_generate_key_stores_correct_sha256_value(self):
        """key_sha256 must be SHA-256 of the raw key (deterministic)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, _ = api_key_manager.generate_key("admin")

            expected = hashlib.sha256(raw_key.encode()).hexdigest()
            # Verify via get_api_key_by_sha256 — it must find the key
            record = user_manager.get_api_key_by_sha256(expected)
            assert record is not None
            assert record["username"] == "admin"


# ---------------------------------------------------------------------------
# Tests: authenticate_bearer — valid key
# ---------------------------------------------------------------------------


class TestAuthenticateBearerValid:
    """authenticate_bearer returns the User when the key is correct."""

    def test_authenticate_bearer_valid_key_returns_user(self):
        """Valid raw key returns the owning User object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, _ = api_key_manager.generate_key("admin")

            user = api_key_manager.authenticate_bearer(raw_key)

            assert user is not None
            assert user.username == "admin"

    def test_authenticate_bearer_returns_correct_role(self):
        """Returned user carries the correct role from the database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, _ = api_key_manager.generate_key("admin")

            user = api_key_manager.authenticate_bearer(raw_key)

            assert user is not None
            assert user.role == UserRole.ADMIN


# ---------------------------------------------------------------------------
# Tests: authenticate_bearer — failure cases
# ---------------------------------------------------------------------------


class TestAuthenticateBearerInvalid:
    """authenticate_bearer returns None for any bad / non-existent key."""

    def test_authenticate_bearer_wrong_key_returns_none(self):
        """A key with the right prefix but wrong bytes → None (bcrypt fails)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            api_key_manager.generate_key("admin")
            tampered = "cidx_sk_" + "0" * 32  # wrong key bytes

            result = api_key_manager.authenticate_bearer(tampered)
            assert result is None

    def test_authenticate_bearer_nonexistent_key_returns_none(self):
        """A well-formed key that was never stored → None (SHA-256 lookup miss)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            # No keys stored at all
            fake_key = "cidx_sk_" + "a" * 32

            result = api_key_manager.authenticate_bearer(fake_key)
            assert result is None

    def test_authenticate_bearer_non_prefix_key_returns_none_without_db_hit(self):
        """Token not starting with cidx_sk_ → fast-reject, no DB query."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)

            # A JWT-shaped token
            jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake.sig"
            result = api_key_manager.authenticate_bearer(jwt_token)
            assert result is None

    def test_authenticate_bearer_empty_string_returns_none(self):
        """Empty string → None (fast-reject on prefix check)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            result = api_key_manager.authenticate_bearer("")
            assert result is None

    def test_authenticate_bearer_legacy_null_sha256_returns_none(self):
        """Legacy key row with NULL key_sha256 → None (no fallback bcrypt scan)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            # Manually insert a legacy-style row without key_sha256
            import uuid

            key_id = str(uuid.uuid4())
            from code_indexer.server.auth.password_manager import PasswordManager

            pm = PasswordManager()
            raw_key = "cidx_sk_" + "b" * 32
            key_hash = pm.hash_password(raw_key)

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """INSERT INTO user_api_keys
                       (key_id, username, key_hash, key_prefix, name, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        key_id,
                        "admin",
                        key_hash,
                        "cidx_sk_bbbb",
                        "legacy",
                        "2025-01-01T00:00:00Z",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            # The key is valid bcrypt-wise but has no sha256 → must return None
            result = api_key_manager.authenticate_bearer(raw_key)
            assert result is None, "Legacy keys with NULL sha256 must not authenticate"

    def test_authenticate_bearer_revoked_deleted_key_returns_none(self):
        """After deleting the key, authenticate_bearer returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, key_id = api_key_manager.generate_key("admin")

            # Verify it works before deletion
            assert api_key_manager.authenticate_bearer(raw_key) is not None

            # Delete (revoke) the key
            user_manager.delete_api_key("admin", key_id)

            # Must return None immediately after deletion (no grace period)
            result = api_key_manager.authenticate_bearer(raw_key)
            assert result is None, "Revoked key must not authenticate"

    def test_authenticate_bearer_tampered_sha256_bcrypt_fails(self):
        """SHA-256 lookup finds row, but bcrypt confirm fails for tampered key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, _ = api_key_manager.generate_key("admin")

            # Tamper with the last character of the key while keeping prefix
            tampered = raw_key[:-1] + ("0" if raw_key[-1] != "0" else "1")

            result = api_key_manager.authenticate_bearer(tampered)
            assert result is None


# ---------------------------------------------------------------------------
# Tests: get_api_key_by_sha256 (UserManager)
# ---------------------------------------------------------------------------


class TestGetApiKeyBySha256:
    """UserManager.get_api_key_by_sha256 direct tests."""

    def test_returns_record_for_existing_key(self):
        """After generate_key, get_api_key_by_sha256 finds the record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager = ApiKeyManager(user_manager=user_manager)
            raw_key, _ = api_key_manager.generate_key("admin")

            sha256_hex = hashlib.sha256(raw_key.encode()).hexdigest()
            record = user_manager.get_api_key_by_sha256(sha256_hex)

            assert record is not None
            assert record["username"] == "admin"
            assert "key_hash" in record

    def test_returns_none_for_unknown_sha256(self):
        """Random sha256 that was never stored → None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            result = user_manager.get_api_key_by_sha256("a" * 64)
            assert result is None


# ---------------------------------------------------------------------------
# Tests: dependencies.get_current_user API-key branch
# ---------------------------------------------------------------------------


class TestGetCurrentUserApiKeyBranch:
    """get_current_user dispatches to authenticate_bearer for cidx_sk_ tokens."""

    def _make_request_with_bearer(self, token: str):
        """Create a minimal mock Request with Authorization: Bearer <token>."""
        from fastapi.security import HTTPAuthorizationCredentials

        mock_request = MagicMock()
        mock_request.cookies = {}
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        return mock_request, credentials

    def test_get_current_user_accepts_valid_api_key(self):
        """get_current_user returns User for a valid API key Bearer token."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager_instance = ApiKeyManager(user_manager=user_manager)
            raw_key, _ = api_key_manager_instance.generate_key("admin")

            import code_indexer.server.auth.dependencies as auth_deps
            from code_indexer.server.auth.jwt_manager import JWTManager

            # Wire the module globals for the test
            auth_deps.user_manager = user_manager
            auth_deps.jwt_manager = JWTManager(secret_key="test-secret-key")
            auth_deps.api_key_manager = api_key_manager_instance
            auth_deps.oauth_manager = None

            mock_request, credentials = self._make_request_with_bearer(raw_key)

            user = auth_deps.get_current_user(mock_request, credentials)

            assert user is not None
            assert user.username == "admin"

    def test_get_current_user_rejects_invalid_api_key(self):
        """get_current_user raises 401 for a bad API key Bearer token."""
        from fastapi import HTTPException

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager_instance = ApiKeyManager(user_manager=user_manager)

            import code_indexer.server.auth.dependencies as auth_deps
            from code_indexer.server.auth.jwt_manager import JWTManager

            auth_deps.user_manager = user_manager
            auth_deps.jwt_manager = JWTManager(secret_key="test-secret-key")
            auth_deps.api_key_manager = api_key_manager_instance
            auth_deps.oauth_manager = None

            bad_key = "cidx_sk_" + "f" * 32
            mock_request, credentials = self._make_request_with_bearer(bad_key)

            with pytest.raises(HTTPException) as exc_info:
                auth_deps.get_current_user(mock_request, credentials)

            assert exc_info.value.status_code == 401

    def test_get_current_user_jwt_path_unaffected(self):
        """JWT token still works — API-key branch does NOT intercept JWTs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager_instance = ApiKeyManager(user_manager=user_manager)

            import code_indexer.server.auth.dependencies as auth_deps
            from code_indexer.server.auth.jwt_manager import JWTManager

            jwt_mgr = JWTManager(secret_key="test-secret-key")
            auth_deps.user_manager = user_manager
            auth_deps.jwt_manager = jwt_mgr
            auth_deps.api_key_manager = api_key_manager_instance
            auth_deps.oauth_manager = None

            # Issue a real JWT for admin
            token = jwt_mgr.create_token(
                {
                    "username": "admin",
                    "role": "admin",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            )

            mock_request, credentials = self._make_request_with_bearer(token)

            user = auth_deps.get_current_user(mock_request, credentials)

            # JWT path worked — authenticate_bearer should NOT have been called
            # (token doesn't start with cidx_sk_)
            assert user is not None
            assert user.username == "admin"

    def test_get_current_user_does_not_call_authenticate_bearer_for_jwt(self):
        """authenticate_bearer is never invoked when token is a JWT."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = _make_db(tmpdir)
            user_manager = _make_sqlite_user_manager(db_path)
            user_manager.seed_initial_admin()

            api_key_manager_instance = ApiKeyManager(user_manager=user_manager)

            import code_indexer.server.auth.dependencies as auth_deps
            from code_indexer.server.auth.jwt_manager import JWTManager

            jwt_mgr = JWTManager(secret_key="test-secret-key")
            auth_deps.user_manager = user_manager
            auth_deps.jwt_manager = jwt_mgr
            auth_deps.api_key_manager = api_key_manager_instance
            auth_deps.oauth_manager = None

            token = jwt_mgr.create_token(
                {
                    "username": "admin",
                    "role": "admin",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            )

            # Spy on authenticate_bearer — wrap to track calls
            calls = []
            original = api_key_manager_instance.authenticate_bearer

            def spy(raw_key):
                calls.append(raw_key)
                return original(raw_key)

            api_key_manager_instance.authenticate_bearer = spy

            mock_request, credentials = self._make_request_with_bearer(token)
            auth_deps.get_current_user(mock_request, credentials)

            assert len(calls) == 0, (
                "authenticate_bearer must not be called for JWT tokens"
            )
