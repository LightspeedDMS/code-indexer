"""
Tests for Story #555: Rate limiting on REST /auth/login endpoint.

Verifies that the login endpoint uses the same TokenBucketManager singleton
as MCP authenticate, returning 429 when rate limited and refunding on success.
"""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.routers.inline_auth import register_auth_routes


def _make_app():
    """Create a minimal FastAPI app with auth routes registered."""
    app = FastAPI()
    mock_jwt = MagicMock()
    mock_jwt.create_access_token.return_value = "test-token"
    mock_user_mgr = MagicMock()
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
    )
    return app, mock_user_mgr


def _make_successful_user(username="alice"):
    """Create a mock user that passes authentication."""
    mock_user = MagicMock()
    mock_user.username = username
    mock_user.role.value = "admin"
    mock_user.created_at.isoformat.return_value = "2026-01-01T00:00:00"
    mock_user.to_dict.return_value = {"username": username, "role": "admin"}
    return mock_user


class TestLoginRateLimiting:
    """Story #555: Rate limiting on /auth/login."""

    @patch("code_indexer.server.routers.inline_auth.rate_limiter")
    def test_returns_429_when_rate_limited(self, mock_rl):
        """Must return 429 with Retry-After when rate limit exceeded."""
        mock_rl.consume.return_value = (False, 4.5)
        app, _ = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.headers["Retry-After"] == "5"  # ceil(4.5)

    @patch("code_indexer.server.routers.inline_auth.rate_limiter")
    def test_consume_called_before_auth(self, mock_rl):
        """consume must be called before credential validation."""
        mock_rl.consume.return_value = (False, 3.0)
        app, mock_user_mgr = _make_app()
        client = TestClient(app)
        client.post("/auth/login", json={"username": "alice", "password": "x"})
        mock_rl.consume.assert_called_once_with("alice")
        mock_user_mgr.authenticate_user.assert_not_called()

    @patch("code_indexer.server.routers.inline_auth.rate_limiter")
    def test_refund_on_successful_login(self, mock_rl):
        """Token must be refunded on successful authentication."""
        mock_rl.consume.return_value = (True, 0.0)
        app, mock_user_mgr = _make_app()
        mock_user_mgr.authenticate_user.return_value = _make_successful_user()

        client = TestClient(app)
        resp = client.post(
            "/auth/login", json={"username": "alice", "password": "correct"}
        )
        assert resp.status_code == 200
        mock_rl.refund.assert_called_once_with("alice")

    @patch("code_indexer.server.routers.inline_auth.rate_limiter")
    def test_no_refund_on_failed_login(self, mock_rl):
        """No refund when credentials are invalid."""
        mock_rl.consume.return_value = (True, 0.0)
        app, mock_user_mgr = _make_app()
        mock_user_mgr.authenticate_user.return_value = None

        client = TestClient(app)
        resp = client.post(
            "/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert resp.status_code == 401
        mock_rl.refund.assert_not_called()

    @patch("code_indexer.server.routers.inline_auth.rate_limiter")
    def test_rate_limit_is_per_username(self, mock_rl):
        """Rate limiting one user must not affect other users."""

        def consume_side_effect(username):
            if username == "alice":
                return (False, 5.0)
            return (True, 0.0)

        mock_rl.consume.side_effect = consume_side_effect
        app, mock_user_mgr = _make_app()
        mock_user_mgr.authenticate_user.return_value = _make_successful_user("bob")

        client = TestClient(app)

        # alice is rate limited
        resp_alice = client.post(
            "/auth/login", json={"username": "alice", "password": "x"}
        )
        assert resp_alice.status_code == 429

        # bob is not affected
        resp_bob = client.post("/auth/login", json={"username": "bob", "password": "x"})
        assert resp_bob.status_code == 200

    @patch("code_indexer.server.routers.inline_auth.rate_limiter")
    def test_429_body_contains_error_message(self, mock_rl):
        """429 response body must contain rate limit error message."""
        mock_rl.consume.return_value = (False, 2.0)
        app, _ = _make_app()
        client = TestClient(app)
        resp = client.post("/auth/login", json={"username": "alice", "password": "x"})
        assert resp.status_code == 429
        body = resp.json()
        assert "too many" in body["detail"].lower()
