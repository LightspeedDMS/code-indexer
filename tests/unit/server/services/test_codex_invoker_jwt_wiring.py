"""
Unit tests for JWT closure wiring into CodexInvoker.

Verifies that the bearer_token_provider closure wired at construction time
produces tokens that JWTManager.validate_token() accepts, that those tokens
carry the expected admin-scope claims, and that each call to the closure
produces a distinct token (jti-based freshness).

Uses real JWTManager (deterministic, fast, no mocks for JWT) — no fakes.

Test inventory (3 tests across 2 classes):

  TestJwtClosureProducesValidToken (2 tests)
    test_closure_token_validates_successfully
    test_closure_token_carries_admin_claims

  TestJwtClosureFreshTokenPerCall (1 test)
    test_closure_produces_different_tokens_on_successive_calls
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest

from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.services.codex_invoker import CodexInvoker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_CODEX_HOME = "/fake/codex-home"
_ADMIN_USERNAME = "admin"
_ADMIN_ROLE = "admin"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jwt_manager() -> JWTManager:
    """
    Return a real JWTManager backed by a cryptographically random secret key.
    A fresh random secret is generated per test invocation — no hardcoded value.
    """
    return JWTManager(secret_key=secrets.token_urlsafe(32), token_expiration_minutes=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_admin_token_provider(jwt_mgr: JWTManager):
    """
    Return a closure that produces admin-scope JWT tokens via jwt_mgr.

    This mirrors the wiring pattern used at the CodexInvoker construction
    site in dependency_map_analyzer.py and description_refresh_scheduler.py.
    """

    def _provide_token() -> str:
        return jwt_mgr.create_token(
            {
                "username": _ADMIN_USERNAME,
                "role": _ADMIN_ROLE,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    return _provide_token


# ---------------------------------------------------------------------------
# Tests: closure produces valid, admin-scoped tokens
# ---------------------------------------------------------------------------


class TestJwtClosureProducesValidToken:
    """The JWT closure wired into CodexInvoker produces tokens JWTManager validates."""

    def test_closure_token_validates_successfully(self, jwt_manager):
        """
        A token produced by the bearer_token_provider closure must be accepted
        by JWTManager.validate_token() without raising TokenExpiredError or
        InvalidTokenError.
        """
        provider = _make_admin_token_provider(jwt_manager)
        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            bearer_token_provider=provider,
        )

        token = invoker._bearer_token_provider()
        payload = jwt_manager.validate_token(token)  # must not raise
        assert payload is not None, "validate_token must return a payload dict"

    def test_closure_token_carries_admin_claims(self, jwt_manager):
        """
        The token produced by the closure must contain username='admin'
        and role='admin' (admin-scope required for MCP tool access).
        """
        provider = _make_admin_token_provider(jwt_manager)
        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            bearer_token_provider=provider,
        )

        token = invoker._bearer_token_provider()
        payload = jwt_manager.validate_token(token)

        assert payload.get("username") == _ADMIN_USERNAME, (
            f"Token must carry username={_ADMIN_USERNAME!r}; got {payload.get('username')!r}"
        )
        assert payload.get("role") == _ADMIN_ROLE, (
            f"Token must carry role={_ADMIN_ROLE!r}; got {payload.get('role')!r}"
        )


# ---------------------------------------------------------------------------
# Tests: closure freshness (jti uniqueness)
# ---------------------------------------------------------------------------


class TestJwtClosureFreshTokenPerCall:
    """The closure produces a fresh token on each call (JWT ID / jti changes)."""

    def test_closure_produces_different_tokens_on_successive_calls(self, jwt_manager):
        """
        Two successive calls to the provider must return distinct token strings.
        JWTs include a jti (JWT ID) UUID that guarantees uniqueness even within
        the same second, so the two tokens must not be equal.
        Both tokens must still pass validation (they are not expired stubs).
        """
        provider = _make_admin_token_provider(jwt_manager)
        invoker = CodexInvoker(
            codex_home=_FAKE_CODEX_HOME,
            bearer_token_provider=provider,
        )

        token_a = invoker._bearer_token_provider()
        token_b = invoker._bearer_token_provider()

        assert token_a != token_b, (
            "Successive provider calls must return distinct tokens (each includes unique jti)"
        )
        # Both must still be valid
        jwt_manager.validate_token(token_a)
        jwt_manager.validate_token(token_b)
