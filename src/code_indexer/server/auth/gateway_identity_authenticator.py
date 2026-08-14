"""EVO-66121: authenticate a gateway-forwarded RS256 JWT into a cidx User.

Bridges :class:`GatewayJwtValidator` (crypto/claims) and ``OIDCManager`` (JIT
provisioning + group mapping). Given a bearer token that the trusted mcp-gateway
forwarded on behalf of an end user:

1. confirm the token is from the trusted gateway (``handles``),
2. verify it and extract the identity claims (``validator.validate``),
3. map those claims to an ``OIDCUserInfo`` and resolve/provision the cidx user via the
   existing ``OIDCManager.match_or_create_user`` path — the SAME JIT + group-membership
   logic interactive SSO users flow through.

cidx then enforces its own per-user access control (groups / ``repo_group_access``) for
the provisioned user, which is the whole point of the passthrough.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from code_indexer.server.auth.gateway_jwt_validator import GatewayTokenError
from code_indexer.server.auth.oidc.oidc_provider import OIDCUserInfo

if TYPE_CHECKING:
    from code_indexer.server.auth.gateway_jwt_validator import GatewayJwtValidator
    from code_indexer.server.auth.user_manager import User


class GatewayIdentityAuthenticator:
    """Turn a validated gateway JWT into a provisioned cidx ``User``."""

    def __init__(self, validator: "GatewayJwtValidator", oidc_manager: object) -> None:
        # oidc_manager is an OIDCManager configured for the trusted gateway (its own
        # issuer/claims/JIT policy/group_mappings), NOT the interactive-SSO instance.
        self._validator = validator
        self._oidc_manager = oidc_manager

    def handles(self, token: str) -> bool:
        """True iff ``token`` is issued by the trusted gateway (cheap iss pre-check)."""
        return bool(self._validator.claims_this_issuer(token))

    async def authenticate(self, token: str) -> "User":
        """Validate ``token`` and resolve/provision the cidx user.

        Precondition: ``handles(token)`` is True (caller routes on it).

        Raises:
            GatewayTokenError: the token failed validation, or the user could not be
                provisioned (JIT disabled, username collision, or email unverified when
                verification is required). The caller maps this to HTTP 401.
        """
        identity = self._validator.validate(token)  # raises GatewayTokenError

        user_info = OIDCUserInfo(
            subject=identity.subject,
            email=identity.email,
            email_verified=identity.email_verified,
            username=identity.username,
            # OIDCUserInfo semantics: absence is None, not [] (matches OIDCProvider).
            groups=identity.groups or None,
        )

        user = await self._oidc_manager.match_or_create_user(user_info)  # type: ignore[attr-defined]
        if user is None:
            raise GatewayTokenError("gateway user could not be provisioned")
        return user
