"""Regression test for #1707: process-wide login rate-limiter singletons
must be reset between every test under tests/unit/server/.

Root cause (confirmed via live repro, 2026-08-27): `token_bucket.rate_limiter`
(TokenBucketManager, Story #555) and `login_rate_limiter.login_rate_limiter`
(LoginRateLimiter, Story #557) are process-wide, module-level singletons with
NO reset mechanism wired into `tests/unit/server/conftest.py` (the auth/
subdirectory's own `reset_singletons` fixture in
`tests/unit/server/auth/conftest.py` does not cover either of these two
singletons, and does not apply outside that subdirectory at all).

`test_health_check_endpoint.py`'s `admin_token` fixture deliberately logs in
with a WRONG password ("admin_password" instead of the real default
"admin") once per test -- 14 real failed login attempts against username
"admin" in one file. `LoginRateLimiter` locks an account out for 15 minutes
after 5 failures in its sliding window, so any LATER test in the same pytest
process that performs a real login as "admin" (e.g.
`test_login_validation.py::test_login_with_valid_credentials_passes_validation`,
every test in `test_placeholder_endpoints_501.py`, and
`test_jwt_restart_persistence_e2e.py`) gets HTTP 429 instead of a real
auth outcome -- confirmed live via
`pytest tests/unit/server/test_health_check_endpoint.py
tests/unit/server/test_placeholder_endpoints_501.py` (15 passed, then 6/6
errors, all "429 Too Many Requests" during setup's real `/auth/login` call).

This test does not depend on running two real files back-to-back (slow,
~15s+ of real create_app() calls each). It exercises the same singletons
directly with a throwaway username, which is enough to prove whether the
tree-wide autouse reset fixture clears LoginRateLimiter/TokenBucketManager
state between tests -- exactly the same shape as the established #1698
TokenBlacklist regression coverage philosophy (verified via the full test
run passing, since the reset function itself has no dedicated unit test in
that fix either).
"""

from code_indexer.server.auth.login_rate_limiter import login_rate_limiter
from code_indexer.server.auth.token_bucket import rate_limiter

_LOCKOUT_USERNAME = "cross-test-pollution-1707-lockout"
_BUCKET_USERNAME = "cross-test-pollution-1707-bucket"

# LoginRateLimiter.__init__'s default max_attempts (login_rate_limiter.py):
# the number of failures within the sliding window that trips a lockout.
_LOCKOUT_MAX_ATTEMPTS = login_rate_limiter._max_attempts

# TokenBucketManager.__init__'s default capacity (token_bucket.py): the
# number of tokens a fresh bucket starts with, i.e. how many consecutive
# consume() calls succeed before the bucket is exhausted.
_BUCKET_CAPACITY = rate_limiter.capacity


class TestLoginLockoutDoesNotLeakAcrossTests:
    """Two tests, in file order, discriminate a leak: test_a trips the
    lockout; test_b (a DIFFERENT test function -- a real cross-test
    boundary, not just sequential calls within one test body) must observe
    a clean slate if the reset fixture works."""

    def test_a_trip_the_lockout(self):
        """Simulate enough failed login attempts to trip LoginRateLimiter's
        lockout threshold. Sanity-checks the lockout mechanism itself
        actually engages, so a failure of test_b can only be explained by
        a missing reset, not by lockout never triggering."""
        for _ in range(_LOCKOUT_MAX_ATTEMPTS):
            login_rate_limiter.check_and_record_failure(_LOCKOUT_USERNAME)
        is_locked, _ = login_rate_limiter.is_locked(_LOCKOUT_USERNAME)
        assert is_locked is True

    def test_b_lockout_from_previous_test_must_not_leak(self):
        """If the tree-wide autouse fixture correctly resets
        LoginRateLimiter state between tests, this username must NOT still
        be locked out from test_a above."""
        is_locked, _ = login_rate_limiter.is_locked(_LOCKOUT_USERNAME)
        assert is_locked is False, (
            "LoginRateLimiter lockout leaked across tests -- the tree-wide "
            "autouse reset fixture in tests/unit/server/conftest.py must "
            "clear login_rate_limiter state between every test (mirrors "
            "the established #1698 TokenBlacklist reset pattern)."
        )


class TestTokenBucketDoesNotLeakAcrossTests:
    """Same shape as above, for the complementary TokenBucketManager burst
    limiter (Story #555)."""

    def test_a_exhaust_the_bucket(self):
        """Consume every token in a fresh bucket WITHOUT refunding (mirrors
        a real failed-login call path, which never calls .refund())."""
        for _ in range(int(_BUCKET_CAPACITY)):
            allowed, _ = rate_limiter.consume(_BUCKET_USERNAME)
            assert allowed is True
        allowed, _ = rate_limiter.consume(_BUCKET_USERNAME)
        assert allowed is False

    def test_b_bucket_from_previous_test_must_not_leak(self):
        """If the tree-wide autouse fixture correctly resets
        TokenBucketManager state between tests, this username must have a
        full, unexhausted bucket."""
        allowed, _ = rate_limiter.consume(_BUCKET_USERNAME)
        assert allowed is True, (
            "TokenBucketManager bucket exhaustion leaked across tests -- "
            "the tree-wide autouse reset fixture in "
            "tests/unit/server/conftest.py must clear token_bucket."
            "rate_limiter state between every test."
        )
