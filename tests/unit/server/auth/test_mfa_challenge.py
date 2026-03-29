"""
Tests for MFA Challenge Token Manager (Story #560).

Tests challenge creation, retrieval, expiry, attempt exhaustion,
and consumption.
"""

import time

import pytest

from code_indexer.server.auth.mfa_challenge import MfaChallengeManager


@pytest.fixture
def manager():
    return MfaChallengeManager(ttl_seconds=300, max_attempts=5)


class TestCreateChallenge:
    def test_returns_token_string(self, manager):
        token = manager.create_challenge("admin", "127.0.0.1")
        assert isinstance(token, str)
        assert len(token) > 20

    def test_stores_username_and_ip(self, manager):
        token = manager.create_challenge("admin", "10.0.0.1", redirect_url="/dash")
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.username == "admin"
        assert challenge.client_ip == "10.0.0.1"
        assert challenge.redirect_url == "/dash"


class TestGetChallenge:
    def test_returns_valid_challenge(self, manager):
        token = manager.create_challenge("alice", "127.0.0.1")
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.username == "alice"

    def test_returns_none_for_unknown_token(self, manager):
        assert manager.get_challenge("nonexistent") is None

    def test_returns_none_for_expired_challenge(self, manager):
        mgr = MfaChallengeManager(ttl_seconds=1, max_attempts=5)
        token = mgr.create_challenge("alice", "127.0.0.1")
        time.sleep(1.1)
        assert mgr.get_challenge(token) is None

    def test_returns_none_when_attempts_exhausted(self, manager):
        token = manager.create_challenge("alice", "127.0.0.1")
        for _ in range(5):
            manager.record_attempt(token)
        assert manager.get_challenge(token) is None


class TestRecordAttempt:
    def test_increments_counter(self, manager):
        token = manager.create_challenge("alice", "127.0.0.1")
        manager.record_attempt(token)
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.attempt_count == 1

    def test_noop_for_unknown_token(self, manager):
        manager.record_attempt("nonexistent")  # Should not raise


class TestConsume:
    def test_removes_and_returns_challenge(self, manager):
        token = manager.create_challenge("alice", "127.0.0.1")
        challenge = manager.consume(token)
        assert challenge is not None
        assert challenge.username == "alice"
        # Token is now gone
        assert manager.get_challenge(token) is None

    def test_returns_none_for_unknown_token(self, manager):
        assert manager.consume("nonexistent") is None
