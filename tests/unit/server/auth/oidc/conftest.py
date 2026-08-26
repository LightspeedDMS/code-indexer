"""
Fixtures for OIDC route tests to ensure the global session manager
(code_indexer.server.web.auth._session_manager) is initialized before
these tests run, and restored to its PRIOR state afterward.

Bug #1673: test_routes.py's sso_callback tests call into
code_indexer.server.web.auth.get_session_manager(), which raises
RuntimeError unless some earlier-executed test module happened to leak
an initialized session manager into the shared module-level global first.
Running test_routes.py standalone (or first in a pytest session) exposed
that implicit load-order dependency.

Fix: save/restore the module-level global exactly as it was found rather
than unconditionally resetting it to None -- this avoids trading a
"fails when run first" bug for a "leaks an initialized session manager
into a LATER test file that expects it uninitialized" bug (this project
has hit that exact "one test isolation fix creates another leak" pattern
before).
"""

import threading

import pytest

import code_indexer.server.web.auth as web_auth_module

# Not a real credential -- purely a cookie-signing key for an
# in-process, no-network unit-test fixture (mirrors the fixture pattern
# in test_user_mfa_routes.py's app_with_user_mfa fixture).
_FIXTURE_COOKIE_SIGNING_KEY = "oidc-conftest-fixture-signing-value-1673"

# Arbitrary, generous fixture timeout (8 hours) -- these tests never
# exercise session expiry, so the exact value is inert; named for
# clarity rather than left as an inline literal.
_FIXTURE_SESSION_TIMEOUT_SECONDS = 8 * 60 * 60

# Guards the save/init/restore sequence below (held for the fixture's
# ENTIRE lifetime, including across yield) so a threaded test runner
# cannot interleave another test's mutation of the shared module-level
# `_session_manager` global between this fixture's save and its
# restore. pytest's default execution model is single-threaded per
# worker process, but this lock makes the sequence race-free regardless
# of runner.
_session_manager_swap_lock = threading.Lock()


class _FakeServerConfig:
    """Minimal server config for SessionManager (mirrors
    test_user_mfa_routes.py's _FakeServerConfig)."""

    host = "127.0.0.1"


class _FakeWebSecurityConfig:
    admin_session_timeout_seconds = _FIXTURE_SESSION_TIMEOUT_SECONDS
    web_session_timeout_seconds = _FIXTURE_SESSION_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def _oidc_session_manager():
    """Initialize the global session manager for OIDC callback tests,
    then restore whatever value (initialized or None) was present
    before this test ran."""
    _session_manager_swap_lock.acquire()
    try:
        previous = web_auth_module._session_manager
        web_auth_module.init_session_manager(
            _FIXTURE_COOKIE_SIGNING_KEY,
            _FakeServerConfig(),
            _FakeWebSecurityConfig(),
        )

        yield

    finally:
        web_auth_module._session_manager = previous
        _session_manager_swap_lock.release()
