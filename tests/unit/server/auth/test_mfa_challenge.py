"""
Tests for MFA Challenge Token Manager (Story #560).

Tests challenge creation, retrieval, expiry, attempt exhaustion,
consumption, IP validation, and TTL enforcement in consume.
"""

import time

import pytest

from code_indexer.server.auth.mfa_challenge import MfaChallengeManager


@pytest.fixture
def manager():
    return MfaChallengeManager(ttl_seconds=300, max_attempts=5)


class TestCreateChallenge:
    def test_returns_token_string(self, manager):
        token = manager.create_challenge("admin", "admin", "127.0.0.1")
        assert isinstance(token, str)
        assert len(token) > 20

    def test_stores_username_role_and_ip(self, manager):
        token = manager.create_challenge(
            "admin", "power_user", "10.0.0.1", redirect_url="/dash"
        )
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.username == "admin"
        assert challenge.role == "power_user"
        assert challenge.client_ip == "10.0.0.1"
        assert challenge.redirect_url == "/dash"


class TestGetChallenge:
    def test_returns_valid_challenge(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.username == "alice"

    def test_returns_none_for_unknown_token(self, manager):
        assert manager.get_challenge("nonexistent") is None

    def test_returns_none_for_expired_challenge(self):
        mgr = MfaChallengeManager(ttl_seconds=1, max_attempts=5)
        token = mgr.create_challenge("alice", "admin", "127.0.0.1")
        time.sleep(1.1)
        assert mgr.get_challenge(token) is None

    def test_returns_none_when_attempts_exhausted(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        for _ in range(5):
            manager.record_attempt(token)
        assert manager.get_challenge(token) is None

    def test_ip_mismatch_returns_none(self, manager):
        token = manager.create_challenge("alice", "admin", "10.0.0.1")
        # Same IP passes
        assert manager.get_challenge(token, client_ip="10.0.0.1") is not None
        # Different IP fails
        assert manager.get_challenge(token, client_ip="192.168.1.1") is None

    def test_ip_none_skips_validation(self, manager):
        token = manager.create_challenge("alice", "admin", "10.0.0.1")
        # No IP = no validation
        assert manager.get_challenge(token, client_ip=None) is not None


class TestRecordAttempt:
    def test_increments_counter(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        manager.record_attempt(token)
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.attempt_count == 1

    def test_noop_for_unknown_token(self, manager):
        manager.record_attempt("nonexistent")  # Should not raise


class TestConsume:
    def test_removes_and_returns_challenge(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        challenge = manager.consume(token)
        assert challenge is not None
        assert challenge.username == "alice"
        assert challenge.role == "admin"
        assert manager.get_challenge(token) is None

    def test_returns_none_for_unknown_token(self, manager):
        assert manager.consume("nonexistent") is None

    def test_returns_none_for_expired_challenge(self):
        """consume() must enforce TTL — expired tokens return None."""
        mgr = MfaChallengeManager(ttl_seconds=1, max_attempts=5)
        token = mgr.create_challenge("alice", "admin", "127.0.0.1")
        time.sleep(1.1)
        assert mgr.consume(token) is None
