"""EVO-66121: validate per-user RS256 JWTs forwarded by a trusted mcp-gateway.

cidx's own credentials are HS256 JWTs (``JWTManager``) + ``cidx_sk_`` API keys. When
cidx is fronted by the mcp-gateway, the gateway forwards each end user's identity as
an RS256 JWT signed with the gateway key (published at the gateway JWKS). This
validator lets cidx trust the gateway as an *external* issuer: it verifies the RS256
signature against the gateway JWKS and the ``iss`` / ``aud`` / ``exp`` claims, then
returns the user's identity claims for JIT provisioning + group mapping (reusing the
existing ``OIDCManager`` path).

Trust is deliberately narrow:

* **Issuer-scoped routing.** ``claims_this_issuer()`` peeks the *unverified* ``iss``
  claim only to decide whether a token is "ours to validate". cidx's own HS256 tokens
  and OAuth opaque tokens never carry the gateway issuer, so they fall through the
  dispatch untouched. The unverified claim is used ONLY for routing, never for auth.
* **Algorithm pinned to RS256.** ``jwt.decode`` is called with
  ``algorithms=["RS256"]`` so an alg-confusion attack (an HS256 token signed with the
  RSA public key as the shared secret) is rejected, not accepted.
* **Audience + issuer + expiry enforced** by ``jwt.decode``; ``sub`` is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

import jwt


class GatewayTokenError(Exception):
    """A token that claims the gateway issuer failed cryptographic/claim validation.

    Callers map this to HTTP 401. It is NOT raised for tokens that simply aren't from
    the gateway — those are filtered out by ``claims_this_issuer()`` first.
    """


@dataclass(frozen=True)
class GatewayIdentity:
    """The per-user identity extracted from a validated gateway JWT."""

    subject: str
    email: Optional[str]
    email_verified: bool
    username: Optional[str]
    groups: List[str]


class GatewayJwtValidator:
    """Validate RS256 JWTs issued by a trusted mcp-gateway against its JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        jwks_uri: str,
        audience: str,
        username_claim: str = "preferred_username",
        email_claim: str = "email",
        groups_claim: str = "groups",
        leeway: int = 60,
        jwks_client: Any = None,
    ) -> None:
        if not issuer:
            raise ValueError("issuer is required")
        if not audience:
            raise ValueError("audience is required")
        self._issuer = issuer
        self._audience = audience
        self._username_claim = username_claim
        self._email_claim = email_claim
        self._groups_claim = groups_claim
        self._leeway = leeway
        # PyJWKClient fetches + caches the gateway's signing keys by kid. Injectable
        # for tests. Constructed lazily-but-eagerly here so a bad jwks_uri surfaces at
        # wiring time, not on the first request.
        self._jwks_client = jwks_client or jwt.PyJWKClient(jwks_uri)

    def claims_this_issuer(self, token: str) -> bool:
        """Return True iff the token's UNVERIFIED ``iss`` equals our gateway issuer.

        Cheap "is this token ours to validate?" pre-check used to route the auth
        dispatch. Never trusts the unverified claim for authentication — a matching
        issuer still goes through full signature verification in ``validate()``.
        """
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return False
        return unverified.get("iss") == self._issuer

    def validate(self, token: str) -> GatewayIdentity:
        """Verify the RS256 signature (via the gateway JWKS) + ``iss``/``aud``/``exp``.

        Raises:
            GatewayTokenError: signature invalid, wrong issuer/audience, expired, or a
                required claim (``exp``/``iss``/``sub``) is missing.
        """
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "sub"]},
            )
        except GatewayTokenError:
            raise
        except Exception as exc:  # PyJWTError, PyJWKClientError, ValueError, ...
            # Never surface the raw token or key material — just the terse reason.
            raise GatewayTokenError(str(exc)) from exc

        subject = claims.get("sub")
        if not subject:
            raise GatewayTokenError("missing sub")

        raw_groups = claims.get(self._groups_claim)
        groups = [str(g) for g in raw_groups] if isinstance(raw_groups, list) else []

        return GatewayIdentity(
            subject=str(subject),
            email=claims.get(self._email_claim),
            email_verified=bool(claims.get("email_verified", False)),
            username=claims.get(self._username_claim),
            groups=groups,
        )
