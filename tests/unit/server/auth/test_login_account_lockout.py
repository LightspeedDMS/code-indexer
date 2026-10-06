"""
Story #557 login rate limiting, reworked as a progressive throttle: a login is
THROTTLED after repeated failures and is never hard-locked out.  Every
attempt is reserved (``begin_attempt``) before its password is checked.

Coverage kept from the original lockout suite, now asserting the throttle:
1. A passed check (``record_success``) resets the attempt counter to 0
2. The threshold attempt (default 5) starts a backoff window (no lockout)
3. Applies to the Web UI login (see test_login_throttle_doors.py)
4. Applies to the REST API login (POST /auth/login)
5. The window elapses on its own (no unlock); it is capped
6. Rate limiting can be disabled via the constructor toggle
7. Audit: the doors write one outcome row per checked attempt and none per
   refusal; asserted on the real audit store in test_login_throttle_doors.py
   and test_login_outcome_rest.py (the limiter's old, never-wired
   audit_logger hook was removed)
8. Throttle is per-username (not per-IP)
9. Attempts older than the window are forgotten

Time is controlled by an injected clock, never by sleeping.  Cluster
(PostgreSQL) behaviour runs against a REAL PostgreSQL in
test_login_throttle.py (postgres-parametrized: cross-node sharing, success
clearing across nodes, concurrent reservations, pruning) and
test_login_lockout_transition_live_pg.py.  Concurrent door requests are
covered in test_login_throttle_doors.py.

ANTI-MOCK: LoginRateLimiter is tested directly with its real (in-memory
SQLite) store; the REST door gets a real token-bucket limiter with room.
Endpoint tests mock only infrastructure (user_manager, jwt_manager,
refresh-token manager).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter
from code_indexer.server.auth.token_bucket import TokenBucketManager


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _attempts(limiter: LoginRateLimiter, count: int, who: str = "alice"):
    outcome = None
    for _ in range(count):
        outcome = limiter.begin_attempt(who)
    return outcome


# ---------------------------------------------------------------------------
# Unit tests for LoginRateLimiter (real implementation, no mocks)
# ---------------------------------------------------------------------------


class TestLoginRateLimiterBasics:
    """Core state machine: attempts, throttle, reset."""

    def test_fresh_user_is_not_throttled(self):
        assert LoginRateLimiter().is_throttled("alice") == (False, 0.0)

    def test_single_attempt_does_not_throttle(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 1)
        assert limiter.is_throttled("alice")[0] is False

    def test_four_attempts_do_not_throttle(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 4)
        assert limiter.is_throttled("alice")[0] is False

    def test_fifth_attempt_starts_a_short_window_not_a_lockout(self):
        """AC2 (reworked): the 5th attempt throttles for 5 s, not 15 min."""
        limiter = LoginRateLimiter()
        outcome = _attempts(limiter, 5)
        assert outcome.admitted is True and outcome.throttle_started is True
        assert 0 < outcome.retry_after_seconds <= 5.0
        assert limiter.is_throttled("alice")[0] is True

    def test_success_resets_attempt_counter(self):
        """AC1: a passed check clears all attempt history."""
        limiter = LoginRateLimiter()
        _attempts(limiter, 4)
        limiter.record_success("alice")
        _attempts(limiter, 4)
        assert limiter.is_throttled("alice")[0] is False

    def test_success_clears_a_running_throttle(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 5)
        limiter.record_success("alice")
        assert limiter.is_throttled("alice")[0] is False

    def test_success_on_unknown_user_is_noop(self):
        limiter = LoginRateLimiter()
        limiter.record_success("nobody")
        assert limiter.is_throttled("nobody")[0] is False


class TestLoginRateLimiterPerUsername:
    """AC8: The throttle is per-username, not shared."""

    def test_throttling_alice_does_not_throttle_bob(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 5)
        assert limiter.is_throttled("alice")[0] is True
        assert limiter.is_throttled("bob")[0] is False

    def test_independent_counters_per_username(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 4)
        _attempts(limiter, 3, who="bob")
        assert limiter.is_throttled("alice")[0] is False
        assert limiter.is_throttled("bob")[0] is False


class TestLoginRateLimiterSlidingWindow:
    """AC9: Attempts older than the window no longer count."""

    def test_expired_attempts_do_not_contribute(self, clock):
        limiter = LoginRateLimiter(window_minutes=5, clock=clock)
        _attempts(limiter, 4)
        clock.advance(5 * 60 + 1)
        assert limiter.begin_attempt("alice").throttle_started is False
        assert limiter.is_throttled("alice")[0] is False

    def test_attempts_within_window_count(self, clock):
        limiter = LoginRateLimiter(window_minutes=5, clock=clock)
        _attempts(limiter, 4)
        clock.advance(5 * 60 - 1)
        assert limiter.begin_attempt("alice").throttle_started is True


class TestLoginRateLimiterWindowExpiry:
    """AC5 (reworked): the window elapses by itself; nobody unlocks it."""

    def test_window_expires_without_unlock(self, clock):
        limiter = LoginRateLimiter(clock=clock)
        _attempts(limiter, 5)
        assert limiter.begin_attempt("alice").admitted is False
        clock.advance(5)
        assert limiter.is_throttled("alice") == (False, 0.0)

    def test_next_attempt_after_window_doubles_it(self, clock):
        limiter = LoginRateLimiter(clock=clock)
        _attempts(limiter, 5)
        clock.advance(5)
        assert limiter.begin_attempt("alice") == (True, 10.0, False)

    def test_window_never_exceeds_the_cap(self, clock):
        limiter = LoginRateLimiter(clock=clock)
        for _ in range(30):
            clock.advance(120)
            limiter.begin_attempt("admin")
        throttled, remaining = limiter.is_throttled("admin")
        assert throttled is True and remaining <= 120


class TestLoginRateLimiterDisabled:
    """AC6: Rate limiting can be disabled via the toggle."""

    def test_disabled_limiter_never_throttles(self):
        limiter = LoginRateLimiter(enabled=False)
        assert _attempts(limiter, 100) == (True, 0.0, False)

    def test_disabled_limiter_is_throttled_returns_false(self):
        assert LoginRateLimiter(enabled=False).is_throttled("alice") == (False, 0.0)

    def test_enabled_limiter_throttles_after_threshold(self):
        limiter = LoginRateLimiter(enabled=True)
        _attempts(limiter, 5)
        assert limiter.is_throttled("alice")[0] is True


class TestLoginRateLimiterConfiguration:
    """Configuration: max_attempts, policy validation and return shapes."""

    def test_custom_max_attempts(self):
        limiter = LoginRateLimiter(max_attempts=3)
        assert _attempts(limiter, 3).throttle_started is True

    def test_custom_max_attempts_2_does_not_throttle_after_2(self):
        limiter = LoginRateLimiter(max_attempts=3)
        _attempts(limiter, 2)
        assert limiter.is_throttled("alice")[0] is False

    def test_begin_attempt_returns_attempt_outcome(self):
        outcome = LoginRateLimiter().begin_attempt("alice")
        assert isinstance(outcome.admitted, bool)
        assert isinstance(outcome.retry_after_seconds, float)
        assert isinstance(outcome.throttle_started, bool)

    def test_invalid_policy_is_rejected(self):
        with pytest.raises(ValueError):
            LoginRateLimiter(max_attempts=0)
        with pytest.raises(ValueError):
            LoginRateLimiter(base_delay_seconds=0)
        with pytest.raises(ValueError):
            LoginRateLimiter(base_delay_seconds=10, max_delay_seconds=5)

    def test_set_sqlite_path_rejects_blank(self):
        with pytest.raises(ValueError):
            LoginRateLimiter().set_sqlite_path("  ")


class TestThrottleTransition:
    def test_transition_is_reported_once(self):
        limiter = LoginRateLimiter(max_attempts=2)
        outcomes = [limiter.begin_attempt("frank") for _ in range(4)]
        assert [o.throttle_started for o in outcomes] == [False, True, False, False]
        assert [o.admitted for o in outcomes] == [True, True, False, False]


# ---------------------------------------------------------------------------
# Integration tests: REST /auth/login endpoint
# ---------------------------------------------------------------------------


def _make_rest_app(login_rate_limiter=None):
    """Create a minimal FastAPI app with auth routes and the throttle."""
    from code_indexer.server.routers.inline_auth import register_auth_routes

    app = FastAPI()
    mock_jwt = MagicMock()
    mock_jwt.create_access_token.return_value = "test-token"
    mock_user_mgr = MagicMock()
    mock_user_mgr.is_password_expired.return_value = False
    mock_refresh_mgr = MagicMock()
    mock_refresh_mgr.create_token_family.return_value = "family-1"
    mock_refresh_mgr.create_initial_refresh_token.return_value = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "refresh_token_expires_in": 604800,
    }
    register_auth_routes(
        app,
        jwt_manager=mock_jwt,
        user_manager=mock_user_mgr,
        refresh_token_manager=mock_refresh_mgr,
        login_rate_limiter=login_rate_limiter,
    )
    return app, mock_user_mgr


def _make_successful_user(username="alice"):
    mock_user = MagicMock()
    mock_user.username = username
    mock_user.role.value = "admin"
    mock_user.created_at.isoformat.return_value = "2026-01-01T00:00:00"
    mock_user.to_dict.return_value = {"username": username, "role": "admin"}
    return mock_user


def _post_login(limiter, password, *, user=None):
    # The separate token-bucket burst limiter (Story #555) is a real one
    # with room, so only the throttle under test can refuse.
    with patch(
        "code_indexer.server.routers.inline_auth.rate_limiter",
        TokenBucketManager(capacity=10_000),
    ):
        app, mock_user_mgr = _make_rest_app(login_rate_limiter=limiter)
        mock_user_mgr.authenticate_user.return_value = user
        resp = TestClient(app).post(
            "/auth/login", json={"username": "alice", "password": password}
        )
    return resp, mock_user_mgr


class TestRestLoginThrottle:
    """AC4: The throttle applies to REST /auth/login."""

    def test_throttled_account_returns_429_with_retry_after(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 5)
        resp, _ = _post_login(limiter, "anything")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "5"

    def test_throttled_response_says_how_long_to_wait(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 5)
        resp, _ = _post_login(limiter, "anything")
        assert resp.json()["detail"] == "Too many attempts, try again in 5 seconds."

    def test_failed_login_is_counted(self):
        limiter = LoginRateLimiter()
        _post_login(limiter, "wrong")
        _attempts(limiter, 4)
        assert limiter.is_throttled("alice")[0] is True

    def test_successful_login_resets_attempt_counter(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 4)
        resp, _ = _post_login(limiter, "correct", user=_make_successful_user())
        assert resp.status_code == 200
        _attempts(limiter, 4)
        assert limiter.is_throttled("alice")[0] is False

    def test_unthrottled_account_can_login(self):
        resp, _ = _post_login(
            LoginRateLimiter(), "correct", user=_make_successful_user()
        )
        assert resp.status_code == 200

    def test_throttle_is_applied_before_auth(self):
        limiter = LoginRateLimiter()
        _attempts(limiter, 5)
        resp, mock_user_mgr = _post_login(
            limiter, "correct", user=_make_successful_user()
        )
        assert resp.status_code == 429
        mock_user_mgr.authenticate_user.assert_not_called()

    def test_correct_password_gets_in_once_the_window_elapses(self, clock):
        limiter = LoginRateLimiter(clock=clock)
        _attempts(limiter, 5)
        clock.advance(5)
        resp, _ = _post_login(limiter, "correct", user=_make_successful_user())
        assert resp.status_code == 200
        assert limiter.is_throttled("alice") == (False, 0.0)

    def test_disabled_limiter_allows_login_after_many_attempts(self):
        limiter = LoginRateLimiter(enabled=False)
        _attempts(limiter, 10)
        resp, _ = _post_login(limiter, "correct", user=_make_successful_user())
        assert resp.status_code == 200
