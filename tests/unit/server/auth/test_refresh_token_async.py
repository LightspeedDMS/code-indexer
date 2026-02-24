"""
Tests for Story #278: RefreshTokenManager lock scope narrowed.

Currently validate_and_rotate_refresh_token() holds self._lock for the entire
method including: hash computation, all SQLite reads, token generation, and
SQLite writes. The lock serializes ALL token operations across ALL users.

Fix: Narrow the lock to cover only the critical read-then-write section
(check if used, mark as used). Token hash computation and new token generation
happen outside the lock.

Key requirements tested:
- validate_and_rotate_refresh_token still detects replay attacks
- Concurrent token rotations for different users produce correct results
- Token hash computation happens outside the lock
- Lock only covers the critical read-then-write section (is_used check + mark used)
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest.mock as mock_module

from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager
from code_indexer.server.auth.jwt_manager import JWTManager


class _InsertTrackingConn:
    """Wraps a sqlite3 connection to track INSERT calls and lock state."""

    def __init__(self, conn, manager, lock_held_log):
        self._conn = conn
        self._manager = manager
        self._lock_held_log = lock_held_log

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).upper()
        if "INSERT INTO REFRESH_TOKENS" in normalized:
            acquired = self._manager._lock.acquire(blocking=False)
            if acquired:
                self._lock_held_log.append(False)  # NOT held
                self._manager._lock.release()
            else:
                self._lock_held_log.append(True)  # WAS held (correct)
        if params is not None:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _TrackingConnContext:
    """Context manager wrapping a real sqlite3 connect() result."""

    def __init__(self, real_ctx, manager, lock_held_log):
        self._real = real_ctx
        self._manager = manager
        self._lock_held_log = lock_held_log

    def __enter__(self):
        conn = self._real.__enter__()
        return _InsertTrackingConn(conn, self._manager, self._lock_held_log)

    def __exit__(self, *args):
        return self._real.__exit__(*args)


def make_manager(tmpdir: str) -> RefreshTokenManager:
    """Create a RefreshTokenManager with a test database."""
    db_path = os.path.join(tmpdir, "test_refresh_tokens.db")
    jwt_manager = JWTManager(secret_key="test-secret-key-for-tests")
    return RefreshTokenManager(
        jwt_manager=jwt_manager,
        db_path=db_path,
        refresh_token_lifetime_days=7,
    )


def create_token_pair(manager: RefreshTokenManager, username: str) -> dict:
    """Helper to create a token family and initial refresh token."""
    family_id = manager.create_token_family(username)
    user_data = {"username": username, "role": "normal_user"}
    return manager.create_initial_refresh_token(family_id, username, user_data)


class TestInsertInsideLock:
    """
    Verify that the new token INSERT happens inside the lock.

    The old token UPDATE (mark used) and the new token INSERT must both be
    inside the same locked section to maintain atomicity. A crash between
    UPDATE and INSERT would leave the token family in an inconsistent state:
    old token consumed but no new token issued.
    """

    def test_new_token_insert_happens_inside_lock(self):
        """New token INSERT must happen while self._lock is held."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)
            token_pair = create_token_pair(manager, "atomic_insert_user")
            refresh_token = token_pair["refresh_token"]

            lock_held_during_insert = []
            # Save real connect BEFORE patching to avoid recursion
            real_connect = sqlite3.connect

            def patched_connect(db, **kwargs):
                real_ctx = real_connect(db, **kwargs)
                return _TrackingConnContext(real_ctx, manager, lock_held_during_insert)

            with mock_module.patch("sqlite3.connect", side_effect=patched_connect):
                result = manager.validate_and_rotate_refresh_token(refresh_token)

            assert result.get("valid") is True, (
                f"Token rotation must succeed: {result}"
            )
            assert len(lock_held_during_insert) >= 1, (
                "INSERT INTO refresh_tokens must be called during token rotation"
            )
            assert all(lock_held_during_insert), (
                "New token INSERT must happen INSIDE the lock to maintain atomicity "
                "with the old token UPDATE. INSERT outside the lock creates a TOCTOU "
                "race: old token consumed but new token not yet stored on crash."
            )


class TestLockScopeNarrowed:
    """Verify lock scope is narrowed in validate_and_rotate_refresh_token."""

    def test_token_hash_computed_outside_lock(self):
        """
        Token hash computation (_hash_token) should happen outside the lock.
        We verify this by checking that the lock is NOT held when _hash_token is called.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            # Create a valid token
            token_pair = create_token_pair(manager, "hashtest_user")
            refresh_token = token_pair["refresh_token"]

            lock_held_during_hash = []

            original_hash = manager._hash_token

            def tracked_hash(token: str) -> str:
                # Check if lock is held when hash is computed
                acquired = manager._lock.acquire(blocking=False)
                if acquired:
                    lock_held_during_hash.append(False)  # Lock was NOT held
                    manager._lock.release()
                else:
                    lock_held_during_hash.append(True)  # Lock WAS held
                return original_hash(token)

            manager._hash_token = tracked_hash

            manager.validate_and_rotate_refresh_token(refresh_token)

            # At least one hash call should have happened outside the lock
            assert len(lock_held_during_hash) >= 1, (
                "_hash_token must be called at least once during token rotation"
            )
            # The first hash call (for the incoming token) should be OUTSIDE the lock
            assert not lock_held_during_hash[0], (
                "Token hash computation must happen OUTSIDE the lock scope. "
                "Moving _hash_token outside the lock reduces serialization."
            )

    def test_new_token_generation_outside_lock(self):
        """
        New token and ID generation (_generate_refresh_token, _generate_secure_id)
        should happen outside the lock.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            token_pair = create_token_pair(manager, "gentest_user")
            refresh_token = token_pair["refresh_token"]

            lock_held_during_generation = []

            original_generate = manager._generate_refresh_token

            def tracked_generate() -> str:
                acquired = manager._lock.acquire(blocking=False)
                if acquired:
                    lock_held_during_generation.append(False)  # NOT held
                    manager._lock.release()
                else:
                    lock_held_during_generation.append(True)  # WAS held
                return original_generate()

            manager._generate_refresh_token = tracked_generate

            manager.validate_and_rotate_refresh_token(refresh_token)

            assert len(lock_held_during_generation) >= 1, (
                "_generate_refresh_token must be called during token rotation"
            )
            assert not lock_held_during_generation[-1], (
                "New refresh token generation must happen OUTSIDE the lock scope"
            )


class TestReplayAttackDetectionPreserved:
    """Verify replay attack detection still works after lock scope change."""

    def test_replay_attack_detected_on_reuse(self):
        """Using an already-used token must trigger replay attack detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            token_pair = create_token_pair(manager, "replay_user")
            refresh_token = token_pair["refresh_token"]

            # First use - should succeed
            result1 = manager.validate_and_rotate_refresh_token(refresh_token)
            assert result1["valid"] is True

            # Second use of same token - must detect replay attack
            result2 = manager.validate_and_rotate_refresh_token(refresh_token)
            assert result2["valid"] is False
            assert result2.get("security_incident") is True

    def test_family_revoked_on_replay_attack(self):
        """On replay attack, the entire token family must be revoked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            token_pair = create_token_pair(manager, "family_revoke_user")
            refresh_token = token_pair["refresh_token"]

            # Use once (valid)
            result1 = manager.validate_and_rotate_refresh_token(refresh_token)
            assert result1["valid"] is True

            new_refresh_token = result1["new_refresh_token"]

            # Replay the original token - triggers family revocation
            manager.validate_and_rotate_refresh_token(refresh_token)

            # New token from the rotated pair should also be invalid now
            result3 = manager.validate_and_rotate_refresh_token(new_refresh_token)
            assert result3["valid"] is False


class TestConcurrentTokenRotationsForDifferentUsers:
    """Verify concurrent rotations for different users work correctly."""

    def test_two_users_rotate_tokens_concurrently(self):
        """
        Two users rotating tokens simultaneously must both succeed.
        With the narrowed lock scope, their operations can overlap.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            # Create tokens for two users
            pair1 = create_token_pair(manager, "user_concurrent_1")
            pair2 = create_token_pair(manager, "user_concurrent_2")

            results = {}
            errors = []

            def rotate_token(username: str, refresh_token: str):
                try:
                    result = manager.validate_and_rotate_refresh_token(refresh_token)
                    results[username] = result
                except Exception as e:
                    errors.append((username, e))

            t1 = threading.Thread(
                target=rotate_token,
                args=("user_concurrent_1", pair1["refresh_token"]),
            )
            t2 = threading.Thread(
                target=rotate_token,
                args=("user_concurrent_2", pair2["refresh_token"]),
            )

            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            assert not errors, f"Concurrent rotation errors: {errors}"
            assert results.get("user_concurrent_1", {}).get("valid") is True, (
                "user_concurrent_1 rotation must succeed"
            )
            assert results.get("user_concurrent_2", {}).get("valid") is True, (
                "user_concurrent_2 rotation must succeed"
            )


class TestTokenValidationStillWorks:
    """Verify basic token validation functionality is preserved."""

    def test_valid_token_rotation_returns_new_tokens(self):
        """Valid token rotation must return new access and refresh tokens."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            token_pair = create_token_pair(manager, "valid_user")
            result = manager.validate_and_rotate_refresh_token(
                token_pair["refresh_token"]
            )

            assert result["valid"] is True
            assert "new_access_token" in result
            assert "new_refresh_token" in result
            assert result["new_refresh_token"] != token_pair["refresh_token"]

    def test_expired_token_returns_invalid(self):
        """Expired token must return valid=False without exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RefreshTokenManager(
                jwt_manager=JWTManager(secret_key="test-key"),
                db_path=os.path.join(tmpdir, "tokens.db"),
                refresh_token_lifetime_days=0,  # Expires immediately
            )

            token_pair = create_token_pair(manager, "expired_user")

            # Wait briefly for token to expire (0-day lifetime = expired now)
            time.sleep(0.01)

            result = manager.validate_and_rotate_refresh_token(
                token_pair["refresh_token"]
            )
            assert result["valid"] is False

    def test_invalid_token_returns_invalid(self):
        """Non-existent token must return valid=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = make_manager(tmpdir)

            result = manager.validate_and_rotate_refresh_token(
                "completely-fake-token-that-does-not-exist"
            )
            assert result["valid"] is False
