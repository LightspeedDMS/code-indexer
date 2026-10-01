"""
Unit tests for Bug #243: JWT token caching in DeploymentExecutor._get_auth_token().

Before the fix, _get_auth_token() cached the token in self._auth_token and returned
the stale cached value on subsequent calls. During long deployments (>10 minutes),
the cached token would expire, causing maintenance API calls to fail with 401.

After the fix: _get_auth_token() generates a fresh token on every call. Token
generation is local JWT signing (no network call), so it's cheap.
"""

from types import SimpleNamespace
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

        class FakeConfigManager:
            def __init__(self, server_dir_path=None):
                pass

            def load_config(self):
                return SimpleNamespace(storage_mode="sqlite", postgres_dsn=None)

        with (
            patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager",
                side_effect=FakeConfigManager,
            ),
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

        class FakeConfigManager:
            def __init__(self, server_dir_path=None):
                pass

            def load_config(self):
                return SimpleNamespace(storage_mode="sqlite", postgres_dsn=None)

        with (
            patch(
                "code_indexer.server.utils.config_manager.ServerConfigManager",
                side_effect=FakeConfigManager,
            ),
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


# Bug #1781: generic placeholder DSN used only as an opaque sentinel value in
# these unit tests - not a real credential or host.
_FAKE_POSTGRES_DSN = "postgresql://test-dsn-placeholder/test_db"


def _invoke_get_auth_token_with_fake_config(executor, config_or_exception, fake_token):
    """Shared scaffolding for TestGetAuthTokenClusterAware cases.

    Patches ServerConfigManager so load_config() either returns
    config_or_exception (a SimpleNamespace-style config object) or raises it
    (when config_or_exception is an Exception instance). Also patches
    JWTSecretManager (spying on server_dir_path/pg_dsn) and JWTManager
    (returning fake_token), then calls executor._get_auth_token().

    Returns (token, captured_args) where captured_args holds the
    server_dir_path/pg_dsn that JWTSecretManager was constructed with, plus
    config_manager_server_dir_path - the server_dir_path that
    ServerConfigManager itself was constructed with (proves the real
    wiring path was exercised, not just its return value).
    """
    captured_args: dict = {}

    class FakeConfigManager:
        def __init__(self, server_dir_path=None):
            captured_args["config_manager_server_dir_path"] = server_dir_path

        def load_config(self):
            if isinstance(config_or_exception, Exception):
                raise config_or_exception
            return config_or_exception

    class FakeSecretManager:
        def __init__(self, server_dir_path=None, pg_dsn=None):
            captured_args["server_dir_path"] = server_dir_path
            captured_args["pg_dsn"] = pg_dsn

        def get_or_create_secret(self):
            return "fake-secret-key"

    with (
        patch(
            "code_indexer.server.utils.config_manager.ServerConfigManager",
            side_effect=FakeConfigManager,
        ),
        patch(
            "code_indexer.server.utils.jwt_secret_manager.JWTSecretManager",
            side_effect=FakeSecretManager,
        ),
        patch(
            "code_indexer.server.auth.jwt_manager.JWTManager",
        ) as MockJWTManager,
    ):
        MockJWTManager.return_value.create_token.return_value = fake_token
        token = executor._get_auth_token()

    return token, captured_args


class TestGetAuthTokenClusterAware:
    """Bug #1781: _get_auth_token() must be cluster-aware.

    In PostgreSQL/cluster mode, the real server signs and validates JWTs
    using a secret stored in the shared PostgreSQL cluster_secrets table
    (see JWTSecretManager(pg_dsn=...) and startup/service_init.py). The
    auto-updater's _get_auth_token() previously always constructed
    JWTSecretManager with no pg_dsn, forcing file-based secret storage even
    in cluster mode - the server then rejected the resulting token with 401.
    """

    def test_get_auth_token_uses_postgres_dsn_in_cluster_mode(self, executor):
        """
        When ServerConfigManager.load_config() reports storage_mode ==
        "postgres" with a postgres_dsn, _get_auth_token() must construct
        JWTSecretManager with pg_dsn=<that dsn> so the token is signed with
        the shared cluster secret instead of a node-local file secret.
        """
        config = SimpleNamespace(
            storage_mode="postgres", postgres_dsn=_FAKE_POSTGRES_DSN
        )

        token, captured_args = _invoke_get_auth_token_with_fake_config(
            executor, config, "fake-cluster-token"
        )

        assert token == "fake-cluster-token", (
            "_get_auth_token() must return the token produced by the mocked "
            f"JWTManager.create_token() call in cluster mode, but got {token!r}."
        )

        assert captured_args.get("pg_dsn") == _FAKE_POSTGRES_DSN, (
            "In cluster (postgres) mode, JWTSecretManager must be constructed "
            "with pg_dsn=<the config's postgres_dsn> so the token is signed "
            "with the shared cluster_secrets-table secret, not a node-local "
            f"file secret. Got pg_dsn={captured_args.get('pg_dsn')!r}."
        )

        assert captured_args.get("config_manager_server_dir_path") == str(
            _cidx_data_dir
        ), (
            "ServerConfigManager must be constructed with server_dir_path="
            f"str(_cidx_data_dir) (= {str(_cidx_data_dir)!r}), proving the "
            "real config-resolution wiring path was exercised. Got "
            f"{captured_args.get('config_manager_server_dir_path')!r}."
        )

    def test_get_auth_token_uses_none_dsn_in_sqlite_mode(self, executor):
        """
        When ServerConfigManager.load_config() reports storage_mode ==
        "sqlite", the pre-existing solo-mode behavior must be preserved
        exactly: JWTSecretManager must be constructed with pg_dsn=None, and
        a valid token must still be returned. This must NOT regress.

        Uses a LEFTOVER truthy postgres_dsn (simulating a demoted cluster
        node or a config template that ships the key) rather than
        postgres_dsn=None - a None dsn does not discriminate against a
        removed `storage_mode == "postgres"` gate, since the outcome would
        be identical either way. The leftover-dsn value is the actual
        boundary input that proves the code ignores a stray dsn when not
        in postgres mode.
        """
        config = SimpleNamespace(
            storage_mode="sqlite",
            postgres_dsn="postgresql://leftover-dsn-should-be-ignored/test_db",
        )

        token, captured_args = _invoke_get_auth_token_with_fake_config(
            executor, config, "fake-sqlite-token"
        )

        assert token == "fake-sqlite-token", (
            "_get_auth_token() must return the token produced by the mocked "
            f"JWTManager.create_token() call in sqlite mode, but got {token!r}."
        )

        assert captured_args.get("pg_dsn") is None, (
            "In sqlite (solo) mode, JWTSecretManager must be constructed "
            "with pg_dsn=None (unchanged pre-#1781 behavior) even when the "
            "config carries a leftover truthy postgres_dsn, but got "
            f"pg_dsn={captured_args.get('pg_dsn')!r}."
        )

        assert captured_args.get("config_manager_server_dir_path") == str(
            _cidx_data_dir
        ), (
            "ServerConfigManager must be constructed with server_dir_path="
            f"str(_cidx_data_dir) (= {str(_cidx_data_dir)!r}), proving the "
            "real config-resolution wiring path was exercised. Got "
            f"{captured_args.get('config_manager_server_dir_path')!r}."
        )

    def test_get_auth_token_returns_none_when_config_load_raises(self, executor):
        """
        Bug #1781 remediation (Fix 1, anti-fallback): when
        ServerConfigManager(...).load_config() itself raises,
        _resolve_jwt_postgres_dsn() must propagate the failure
        (RuntimeError) instead of silently falling back to pg_dsn=None.
        That RuntimeError is caught by _get_auth_token()'s own pre-existing
        outer `except Exception` handler, which returns None (deny the
        token) rather than proceeding to sign with a node-local file
        secret the cluster cannot validate. Callers already handle token
        is None by skipping maintenance-mode entry and proceeding with the
        restart anyway - this is a genuine behavior change from the
        pre-remediation test: fail loud, not quietly proceed with a wrong
        token.
        """
        token, captured_args = _invoke_get_auth_token_with_fake_config(
            executor,
            RuntimeError("simulated config load failure"),
            "fake-fallback-token",
        )

        assert token is None, (
            "When ServerConfigManager(...).load_config() raises, "
            "_get_auth_token() must fail loud and return None instead of "
            f"silently proceeding with a wrong secret. Got {token!r}."
        )

        assert "pg_dsn" not in captured_args, (
            "JWTSecretManager must never be constructed when config "
            "loading fails - the RuntimeError must propagate before "
            f"reaching that point. Got captured_args={captured_args!r}."
        )

    def test_get_auth_token_returns_none_when_postgres_mode_missing_dsn(self, executor):
        """
        Bug #1781 remediation (Fix 1, anti-fallback): postgres storage_mode
        configured without a postgres_dsn is an operator misconfiguration,
        not a legitimate standalone-mode signal.
        _resolve_jwt_postgres_dsn() must raise (RuntimeError) rather than
        silently falling back to file-based JWT secret storage - which
        would sign tokens the cluster's PostgreSQL secret can never
        validate. _get_auth_token()'s outer handler catches this and
        returns None.
        """
        config = SimpleNamespace(storage_mode="postgres", postgres_dsn="")

        token, captured_args = _invoke_get_auth_token_with_fake_config(
            executor, config, "fake-token-should-not-be-returned"
        )

        assert token is None, (
            "postgres mode with an empty postgres_dsn must be treated as "
            f"a failure (RuntimeError -> None), not a valid token. Got "
            f"{token!r}."
        )

        assert "pg_dsn" not in captured_args, (
            "JWTSecretManager must never be constructed when postgres "
            "mode is missing its dsn - the RuntimeError must propagate "
            f"before reaching that point. Got captured_args={captured_args!r}."
        )
