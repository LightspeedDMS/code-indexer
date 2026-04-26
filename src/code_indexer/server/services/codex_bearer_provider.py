"""
Shared factory for admin-scope JWT bearer token closures used by CodexInvoker.

Bug #898 / v9.23.9: CodexInvoker's HTTP MCP transport reads CIDX_MCP_BEARER_TOKEN
from the subprocess environment at startup. Without a wired bearer_token_provider
the env var is never set and codex cannot authenticate against the cidx-local MCP
HTTP endpoint. This module provides the canonical factory used at both production
wiring sites (DependencyMapAnalyzer and DescriptionRefreshScheduler).

JWT TTL equals jwt_expiration_minutes from runtime config (default 10 min). A
single codex pass that exceeds the TTL will see 401 on subsequent MCP calls —
configure jwt_expiration_minutes >= pass timeout to avoid mid-run auth failures.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.utils.jwt_secret_manager import JWTSecretManager

logger = logging.getLogger(__name__)

# Default JWT expiration when runtime config does not expose jwt_expiration_minutes.
# Matches the JWTManager default and the deployment_executor.py precedent.
_DEFAULT_JWT_EXPIRATION_MINUTES = 10


def _resolve_jwt_expiration(config: object) -> int:
    """Extract and validate jwt_expiration_minutes from config.

    Args:
        config: ServerConfig returned by get_config_service().get_config().

    Returns:
        A positive integer expiration in minutes.

    Raises:
        ValueError: When jwt_expiration_minutes is present but invalid (not a
            positive non-bool integer).
    """
    raw = getattr(config, "jwt_expiration_minutes", None)

    if raw is None:
        # Setting absent — use default, warn so operators know the fallback fired.
        logger.warning(
            "build_codex_bearer_provider: jwt_expiration_minutes not found in config; "
            "defaulting to %d minutes",
            _DEFAULT_JWT_EXPIRATION_MINUTES,
        )
        return _DEFAULT_JWT_EXPIRATION_MINUTES

    # Reject bool (bool is a subclass of int in Python — exclude explicitly).
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(
            f"build_codex_bearer_provider: jwt_expiration_minutes must be a positive int, "
            f"got {raw!r} (type={type(raw).__name__})"
        )

    return raw


def build_codex_bearer_provider() -> Callable[[], str]:
    """Return a closure that mints a fresh admin-scope JWT per invocation.

    The closure is called once per CodexInvoker.invoke() call (just before the
    subprocess is spawned) so the token is always fresh relative to the jwt TTL.

    Imports are deferred inside the closure so this module can be imported in
    pure-CLI contexts where the server package is available but may not be fully
    initialised. The imports are cheap after the first call (Python module cache).

    Returns:
        A zero-argument callable that returns a valid JWT string each time it is
        called. The token carries role='admin' and username='admin' claims and is
        signed with the server's persistent JWT secret.

    Wires bearer_token_provider for cidx-local MCP authentication via fresh
    admin-scope JWT (TTL = jwt_expiration_minutes runtime config, fallback
    _DEFAULT_JWT_EXPIRATION_MINUTES minutes when the setting is absent).
    """

    def _provider() -> str:
        secret = JWTSecretManager().get_or_create_secret()
        config = get_config_service().get_config()
        expiration = _resolve_jwt_expiration(config)

        manager = JWTManager(
            secret_key=secret,
            token_expiration_minutes=expiration,
        )
        return manager.create_token(
            {
                "username": "admin",
                "role": "admin",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    return _provider
