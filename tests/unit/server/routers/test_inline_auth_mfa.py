"""
Tests for REST MFA enforcement on /auth/login and /auth/mfa/verify (Story #561).

Tests:
- Login returns mfa_required + mfa_token when user has MFA enabled
- Login returns normal tokens when MFA not enabled
- Login returns normal tokens when TOTP service is None
- /auth/mfa/verify with valid TOTP code returns JWT tokens
- /auth/mfa/verify with valid recovery code returns JWT tokens
- /auth/mfa/verify with invalid code returns 401
- /auth/mfa/verify with expired/invalid token returns 401
- /auth/mfa/verify with IP mismatch returns 401
- /auth/mfa/verify with no code provided returns 401
- /auth/mfa/verify when TOTP service unavailable returns 503
- /auth/mfa/verify consumes token (single use)
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.mfa_challenge import MfaChallengeManager
from code_indexer.server.auth.user_manager import User, UserRole

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
)

# secrets.token_urlsafe(32) produces 43-char strings; 20 is a safe lower bound
_MIN_TOKEN_LENGTH = 20


# ---------------------------------------------------------------------------
# Fakes (real logic, no mocking)
# ---------------------------------------------------------------------------


class FakeUserManager:
    """Returns a predetermined user on authenticate_user."""

    def __init__(self, user_to_return):
        self._user = user_to_return

    def authenticate_user(self, username, password):
        if username == self._user.username:
            return self._user
        return None

    def get_user(self, username):
        if username == self._user.username:
            return self._user
        return None


class FakeRefreshTokenManager:
    """Returns predetermined token data."""

    def create_token_family(self, username):
        return "fake-family-id"

    def create_initial_refresh_token(self, family_id, username, user_data):
        return {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "refresh_token_expires_in": 86400,
        }


class FakeTOTPService:
    """Fake TOTP service with deterministic verify behavior."""

    def __init__(self, mfa_enabled_users=None):
        self._mfa_enabled = set(mfa_enabled_users or [])

    def is_mfa_enabled(self, username):
        return username in self._mfa_enabled

    def verify_code(self, username, code):
        return code == "123456"

    def verify_recovery_code(self, username, code, ip_address="unknown"):
        return code == "AAAA-BBBB-CCCC-DDDD"


class FakeRateLimiter:
    """Always allows requests."""

    def consume(self, key):
        return True, 0.0

    def refund(self, key):
        pass


class FakeLockoutLimiter:
    """Never locks out."""

    def is_locked(self, username):
        return False, 0.0

    def record_success(self, username):
        pass

    def check_and_record_failure(self, username):
        pass


class FakeAuthErrorHandler:
    """Minimal auth error handler with timing prevention pass-through."""

    class _TimingPassthrough:
        def constant_time_execute(self, func):
            return func()

    timing_prevention = _TimingPassthrough()

    def perform_dummy_password_work(self):
        pass

    def create_error_response(self, error_type, username, **kwargs):
        return {"status_code": 401, "message": "Invalid credentials"}


# ---------------------------------------------------------------------------
# Context managers to reduce duplication (Messi Rule #4)
# ---------------------------------------------------------------------------


@contextmanager
def patch_mfa_globals(totp_svc=None, rate_limiter=None, auth_handler=None):
    """Temporarily replace module-level globals used by auth routes."""
    import code_indexer.server.web.mfa_routes as mfa_mod
    import code_indexer.server.auth.token_bucket as tb_mod
    import code_indexer.server.auth.auth_error_handler as aeh_mod

    orig_totp = mfa_mod._totp_service
    orig_rl = tb_mod.rate_limiter
    orig_aeh = aeh_mod.auth_error_handler

    mfa_mod._totp_service = totp_svc
    if rate_limiter is not None:
        tb_mod.rate_limiter = rate_limiter
    if auth_handler is not None:
        aeh_mod.auth_error_handler = auth_handler
    try:
        yield
    finally:
        mfa_mod._totp_service = orig_totp
        tb_mod.rate_limiter = orig_rl
        aeh_mod.auth_error_handler = orig_aeh


@contextmanager
def patch_login_handler(user, totp_svc=None):
    """Patch the /auth/login handler closure vars AND module globals."""
    handler = _find_route_handler("/auth/login", "POST")
    with (
        _patch_closure(handler, "user_manager", FakeUserManager(user)),
        _patch_closure(handler, "refresh_token_manager", FakeRefreshTokenManager()),
        _patch_closure(handler, "_lockout_limiter", FakeLockoutLimiter()),
        patch_mfa_globals(
            totp_svc=totp_svc,
            rate_limiter=FakeRateLimiter(),
            auth_handler=FakeAuthErrorHandler(),
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_mfa_user() -> User:
    return User(
        username="mfauser",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _make_normal_user() -> User:
    return User(
        username="normaluser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests: /auth/login MFA challenge
# ---------------------------------------------------------------------------


class TestLoginMfaChallenge:
    """POST /auth/login returns MFA challenge when MFA is enabled."""

    def test_login_returns_mfa_required_when_mfa_enabled(self):
        """Login with MFA-enabled user returns mfa_required and mfa_token."""
        user = _make_mfa_user()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        with patch_login_handler(user, totp_svc=totp_svc):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/auth/login",
                json={"username": "mfauser", "password": "correct"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["mfa_required"] is True
            assert "mfa_token" in data
            assert len(data["mfa_token"]) > _MIN_TOKEN_LENGTH
            assert "access_token" not in data

    def test_login_returns_tokens_when_mfa_not_enabled(self):
        """Login without MFA returns normal JWT tokens."""
        user = _make_normal_user()
        totp_svc = FakeTOTPService(mfa_enabled_users=[])

        with patch_login_handler(user, totp_svc=totp_svc):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/auth/login",
                json={"username": "normaluser", "password": "correct"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data
            assert data["token_type"] == "bearer"

    def test_login_returns_tokens_when_totp_service_none(self):
        """When TOTP service not initialized, login returns normal tokens."""
        user = _make_normal_user()

        with patch_login_handler(user, totp_svc=None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/auth/login",
                json={"username": "normaluser", "password": "correct"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data


# ---------------------------------------------------------------------------
# Tests: /auth/mfa/verify endpoint
# ---------------------------------------------------------------------------


class TestMfaVerifyEndpoint:
    """POST /auth/mfa/verify verifies TOTP/recovery and returns JWT tokens."""

    def _create_challenge(
        self, username="mfauser", role="admin", client_ip="testclient"
    ):
        """Create a challenge token and return (manager, token)."""
        mgr = MfaChallengeManager(ttl_seconds=300, max_attempts=5)
        token = mgr.create_challenge(username=username, role=role, client_ip=client_ip)
        return mgr, token

    def _verify_request(self, mgr, totp_svc, payload):
        """Send POST /auth/mfa/verify with given manager and payload."""
        handler = _find_route_handler("/auth/mfa/verify", "POST")
        with (
            _patch_closure(handler, "_mfa_challenge_mgr", mgr),
            patch_mfa_globals(totp_svc=totp_svc),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            return client.post("/auth/mfa/verify", json=payload)

    def test_verify_with_valid_totp_returns_tokens(self):
        """Valid TOTP code returns JWT access and refresh tokens."""
        mgr, token = self._create_challenge()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        resp = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
                "totp_code": "123456",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["username"] == "mfauser"

    def test_verify_with_valid_recovery_code_returns_tokens(self):
        """Valid recovery code returns JWT tokens."""
        mgr, token = self._create_challenge()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        resp = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
                "recovery_code": "AAAA-BBBB-CCCC-DDDD",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["user"]["username"] == "mfauser"

    def test_verify_with_invalid_code_returns_401(self):
        """Invalid TOTP code returns 401."""
        mgr, token = self._create_challenge()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        resp = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
                "totp_code": "000000",
            },
        )
        assert resp.status_code == 401

    def test_verify_with_invalid_token_returns_401(self):
        """Nonexistent mfa_token returns 401."""
        mgr = MfaChallengeManager()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        resp = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": "nonexistent-token",
                "totp_code": "123456",
            },
        )
        assert resp.status_code == 401

    def test_verify_with_ip_mismatch_returns_401(self):
        """Challenge created with one IP, verified from another, returns 401."""
        mgr, token = self._create_challenge(client_ip="10.0.0.1")
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        # TestClient uses "testclient" as client IP, which differs from 10.0.0.1
        resp = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
                "totp_code": "123456",
            },
        )
        assert resp.status_code == 401

    def test_verify_with_no_code_returns_401(self):
        """Request with neither totp_code nor recovery_code returns 401."""
        mgr, token = self._create_challenge()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        resp = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
            },
        )
        assert resp.status_code == 401

    def test_verify_when_totp_service_unavailable_returns_503(self):
        """When TOTP service is None, returns 503."""
        mgr, token = self._create_challenge()

        resp = self._verify_request(
            mgr,
            None,
            {
                "mfa_token": token,
                "totp_code": "123456",
            },
        )
        assert resp.status_code == 503

    def test_verify_consumes_token_single_use(self):
        """After successful verify, same token cannot be reused."""
        mgr, token = self._create_challenge()
        totp_svc = FakeTOTPService(mfa_enabled_users=["mfauser"])

        resp1 = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
                "totp_code": "123456",
            },
        )
        assert resp1.status_code == 200

        resp2 = self._verify_request(
            mgr,
            totp_svc,
            {
                "mfa_token": token,
                "totp_code": "123456",
            },
        )
        assert resp2.status_code == 401
