"""EVO-66121: build the trusted-gateway authenticator from config (testable seam).

Extracted from the lifespan startup block so the enabled / disabled / fail-safe wiring
is unit-testable without spinning up the whole app. Constructs the RS256 validator + a
gateway-scoped OIDCManager (its OWN identity-link store, isolated from interactive SSO)
+ the authenticator, only when ``trusted_gateway_config.enabled``. Any construction error
logs and returns None -- the caller must treat None as "gateway auth off", never leave a
partially-wired authenticator.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import TrustedGatewayConfig
    from .gateway_identity_authenticator import GatewayIdentityAuthenticator

logger = logging.getLogger(__name__)


async def build_gateway_authenticator(
    trusted_gateway_config: "Optional[TrustedGatewayConfig]",
    *,
    user_manager,
    jwt_manager,
    group_manager=None,
    oidc_db_path: Optional[str] = None,
) -> "Optional[GatewayIdentityAuthenticator]":
    """Return a GatewayIdentityAuthenticator when the gateway trust is enabled, else None.

    Args:
        trusted_gateway_config: the ``TrustedGatewayConfig`` (None / disabled -> None).
        user_manager: the app-wired (Postgres-backed) UserManager.
        jwt_manager: the app-wired JWTManager (unused by the bearer path, required by
            OIDCManager's constructor).
        group_manager: GroupAccessManager for SSO group provisioning (optional).
        oidc_db_path: override the gateway's identity-link store path (tests pass a tmp
            file). Defaults to a gateway-specific ``gateway_oauth.db`` -- deliberately NOT
            the shared ``oauth.db`` interactive SSO uses, so the two trust domains' subjects
            can never collide (MR !49 review finding).

    Returns:
        The authenticator, or None on disabled/misconfigured/error (fail-safe).
    """
    tg = trusted_gateway_config
    if tg is None or not getattr(tg, "enabled", False):
        return None

    try:
        from .gateway_jwt_validator import GatewayJwtValidator
        from .gateway_identity_authenticator import GatewayIdentityAuthenticator
        from .oidc.oidc_manager import OIDCManager

        validator = GatewayJwtValidator(
            issuer=tg.issuer,
            jwks_uri=tg.jwks_uri,
            audience=tg.audience,
            username_claim=tg.username_claim,
            email_claim=tg.email_claim,
            groups_claim=tg.groups_claim,
            leeway=tg.leeway_seconds,
        )

        db_path = oidc_db_path or str(Path.home() / ".cidx-server" / "gateway_oauth.db")
        oidc = OIDCManager(
            config=tg,
            user_manager=user_manager,
            jwt_manager=jwt_manager,
            db_path=db_path,  # isolated identity-link store (distinct trust domain)
        )
        await oidc.initialize()
        if group_manager is not None:
            # group_manager is injected dynamically (OIDCManager guards it via hasattr),
            # mirroring the interactive-SSO wiring in lifespan.
            setattr(oidc, "group_manager", group_manager)

        return GatewayIdentityAuthenticator(validator, oidc)
    except Exception:
        # Fail safe: never leave a half-wired authenticator. Gateway auth stays off.
        logger.error(
            "EVO-66121: failed to build trusted-gateway authenticator; gateway auth "
            "disabled",
            exc_info=True,
        )
        return None
