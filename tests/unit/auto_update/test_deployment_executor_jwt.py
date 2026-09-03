"""
Unit tests for Bug #243: JWT token caching in DeploymentExecutor._get_auth_token().

Before the fix, _get_auth_token() cached the token in self._auth_token and returned
the stale cached value on subsequent calls. During long deployments (>10 minutes),
the cached token would expire, causing maintenance API calls to fail with 401.

After the fix: _get_auth_token() generates a fresh token on every call. Token
generation is local JWT signing (no network call), so it's cheap.
"""

from unittest.mock import patch

import pytest

from code_indexer.server.auto_update.deployment_executor import (
    DeploymentExecutor,
    _cidx_data_dir,
)


@pytest.fixture
def executor(tmp_path):
    """Create a DeploymentExecutor instance for testing."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
        server_url="http://localhost:8000",
        drain_poll_interval=0,
    )


class TestGetAuthTokenNoCaching:
    """Bug #243: _get_auth_token() must generate a fresh token on every call."""

    def test_get_auth_token_generates_fresh_each_call(self, executor):
        """
        Calling _get_auth_token() twice must instantiate JWTManager twice,
        confirming a fresh manager is created per call with no caching.

        With the old caching code, the second call would return the identical
        cached string without calling JWTSecretManager or JWTManager at all.
        """
        call_count = {"jwt_manager": 0}

        class FakeJWTManager:
            def __init__(self, secret_key, token_expiration_minutes):
                call_count["jwt_manager"] += 1

            def create_token(self, payload):
                return f"token_{call_count['jwt_manager']}"

        class FakeSecretManager:
            def get_or_create_secret(self):
                return "fake-secret-key"

        with (
            patch(
                "code_indexer.server.utils.jwt_secret_manager.JWTSecretManager",
                return_value=FakeSecretManager(),
            ),
            patch(
                "code_indexer.server.auth.jwt_manager.JWTManager",
                side_effect=lambda **kwargs: FakeJWTManager(**kwargs),
            ),
        ):
            token1 = executor._get_auth_token()
            token2 = executor._get_auth_token()

        # JWTManager must have been instantiated twice (once per call)
        assert call_count["jwt_manager"] == 2, (
            f"Expected JWTManager to be instantiated twice (fresh per call), "
            f"but got {call_count['jwt_manager']} instantiations. "
            "Token caching may still be in effect."
        )

        # Tokens must be different (each call creates a new manager instance)
        assert token1 != token2, (
            "Two calls to _get_auth_token() must produce different tokens "
            "when fresh JWTManager instances are used. Identical tokens suggest "
            "the cached value is being returned."
        )

    def test_get_auth_token_no_cached_attribute(self, executor):
        """
        DeploymentExecutor must NOT have a _auth_token instance variable
        (the caching mechanism has been removed in Bug #243 fix).
        """
        assert not hasattr(executor, "_auth_token"), (
            "DeploymentExecutor must not have a _auth_token attribute. "
            "Token caching was removed in Bug #243."
        )

    def test_get_auth_token_returns_none_when_secret_missing(self, executor):
        """
        When JWTSecretManager raises FileNotFoundError (server not initialized),
        _get_auth_token() must return None and not propagate the exception.
        """
        with patch(
            "code_indexer.server.utils.jwt_secret_manager.JWTSecretManager"
        ) as MockSecretManager:
            MockSecretManager.return_value.get_or_create_secret.side_effect = (
                FileNotFoundError("JWT secret file not found")
            )

            result = executor._get_auth_token()

        assert result is None, (
            "When JWT secret file is missing (FileNotFoundError), "
            "_get_auth_token() must return None, not raise."
        )


class TestGetAuthTokenUsesCidxDataDir:
    """Bug #1779: JWTSecretManager must be constructed with the auto-updater's
    own resolved data-directory constant (_cidx_data_dir), not its own
    independent default resolution, so IPC paths stay consistent with
    LAUNCH_CONFIG_PATH / PENDING_REDEPLOY_MARKER etc.
    """

    def test_get_auth_token_constructs_jwt_secret_manager_with_cidx_data_dir(
        self, executor
    ):
        """
        JWTSecretManager() must be called with server_dir_path=str(_cidx_data_dir)
        explicitly, instead of relying on JWTSecretManager's own independent
        default resolution (which could diverge if CIDX_SERVER_DATA_DIR and
        CIDX_DATA_DIR are ever set differently).
        """
        captured_args = {}

        class FakeSecretManager:
            def __init__(self, server_dir_path=None, pg_dsn=None):
                captured_args["server_dir_path"] = server_dir_path

            def get_or_create_secret(self):
                return "fake-secret-key"

        with (
            patch(
                "code_indexer.server.utils.jwt_secret_manager.JWTSecretManager",
                side_effect=FakeSecretManager,
            ),
            patch(
                "code_indexer.server.auth.jwt_manager.JWTManager",
            ) as MockJWTManager,
        ):
            MockJWTManager.return_value.create_token.return_value = "fake-token"
            token = executor._get_auth_token()

        assert token == "fake-token", (
            "_get_auth_token() must return the token produced by the mocked "
            "JWTManager.create_token() call. A mismatch (or None) here means "
            "the broad `except Exception: return None` in _get_auth_token() "
            "silently swallowed a downstream failure after the JWTSecretManager "
            f"constructor call, but got {token!r}."
        )

        assert captured_args.get("server_dir_path") == str(_cidx_data_dir), (
            "JWTSecretManager must be constructed with server_dir_path="
            f"str(_cidx_data_dir) (= {str(_cidx_data_dir)!r}), but got "
            f"{captured_args.get('server_dir_path')!r}. This makes the "
            "auto-updater's JWT secret resolution consistent with its own "
            "other IPC paths."
        )
