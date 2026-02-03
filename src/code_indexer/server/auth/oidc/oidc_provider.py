"""OIDC provider implementation for generic OIDC-compliant providers."""

from code_indexer.server.middleware.correlation import get_correlation_id
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class OIDCMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str

    userinfo_endpoint: Optional[str] = None


@dataclass
class OIDCUserInfo:
    subject: str
    email: Optional[str] = None
    email_verified: bool = False
    username: Optional[str] = None
    groups: Optional[List[str]] = None


class OIDCProvider:
    def __init__(self, config):
        self.config = config
        self._metadata = None

    async def discover_metadata(self):
        import httpx

        # Construct well-known URL
        well_known_url = f"{self.config.issuer_url}/.well-known/openid-configuration"

        # Fetch metadata from well-known endpoint
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(well_known_url)
                response.raise_for_status()  # Raise HTTPStatusError for 4xx/5xx
                data = response.json()  # Not async in httpx
        except httpx.HTTPStatusError as e:
            raise Exception(
                f"Failed to discover OIDC metadata: HTTP {e.response.status_code} - {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise Exception(
                f"Failed to connect to OIDC provider at {well_known_url}: {str(e)}"
            ) from e

        # Create and return OIDCMetadata
        metadata = OIDCMetadata(
            issuer=data["issuer"],
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            userinfo_endpoint=data.get("userinfo_endpoint"),
        )

        return metadata

    def get_authorization_url(self, state, redirect_uri, code_challenge):
        from urllib.parse import urlencode

        # Build query parameters for OIDC authorization request
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(self.config.scopes),
        }

        # Build full authorization URL
        query_string = urlencode(params)
        auth_url = f"{self._metadata.authorization_endpoint}?{query_string}"

        return auth_url

    async def exchange_code_for_token(self, code, code_verifier, redirect_uri):
        import httpx

        # Build token request payload
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code_verifier": code_verifier,
        }

        # Exchange code for tokens
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self._metadata.token_endpoint, data=data)
                response.raise_for_status()  # Raise HTTPStatusError for 4xx/5xx
                tokens = response.json()  # Not async in httpx
        except httpx.HTTPStatusError as e:
            raise Exception(
                f"Failed to exchange authorization code for token: HTTP {e.response.status_code} - {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise Exception(f"Failed to connect to token endpoint: {str(e)}") from e

        # Validate token response has required fields
        if "access_token" not in tokens:
            raise Exception("Invalid token response: missing access_token field")

        return tokens

    def get_user_info(self, access_token, id_token):
        """Parse ID token to extract user information and claims.

        ID token contains all necessary user claims including groups.
        This approach works universally with Entra, Keycloak, and other OIDC providers.

        NOTE: This is intentionally a sync function (not async) because it performs
        no I/O operations - only in-memory JWT parsing (base64 decode + JSON parse).

        Args:
            access_token: OAuth access token (kept for backward compatibility)
            id_token: OIDC ID token (JWT) containing user claims

        Returns:
            OIDCUserInfo object with user claims including groups
        """
        import base64
        import json
        import logging

        logger = logging.getLogger(__name__)

        # Parse ID token JWT (format: header.payload.signature)
        if not id_token:
            raise Exception("ID token is required but was not provided")

        try:
            parts = id_token.split(".")
            if len(parts) != 3:
                raise Exception(
                    f"Invalid ID token format: expected 3 parts, got {len(parts)}"
                )

            # Decode payload (base64url decode with padding)
            payload = parts[1]
            # Add padding if needed (base64 requires length to be multiple of 4)
            padding = 4 - (len(payload) % 4)
            if padding != 4:
                payload += "=" * padding

            data = json.loads(base64.urlsafe_b64decode(payload))
            logger.info(
                f"Parsed ID token with claims: {list(data.keys())}",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            raise Exception(f"Failed to parse ID token: {e}") from e

        # Validate ID token has required fields
        if "sub" not in data or not data["sub"]:
            raise Exception("Invalid ID token: missing or empty sub (subject) claim")

        # Log claim extraction for debugging
        logger.info(
            f"Extracting claims - email_claim: {self.config.email_claim}, username_claim: {self.config.username_claim}",
            extra={"correlation_id": get_correlation_id()},
        )
        logger.info(
            f"Available claims in ID token: {list(data.keys())}",
            extra={"correlation_id": get_correlation_id()},
        )

        email_value = data.get(self.config.email_claim)
        logger.info(
            f"Extracted email from '{self.config.email_claim}' claim: {email_value}",
            extra={"correlation_id": get_correlation_id()},
        )

        username_value = data.get(self.config.username_claim)
        logger.info(
            f"Extracted username from '{self.config.username_claim}' claim: {username_value}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Extract groups from configured groups_claim
        groups_value = data.get(self.config.groups_claim)
        logger.info(
            f"Groups claim '{self.config.groups_claim}' raw value: {groups_value} (type: {type(groups_value).__name__})",
            extra={"correlation_id": get_correlation_id()},
        )
        if groups_value and isinstance(groups_value, list):
            groups_list = [str(g) for g in groups_value]
            logger.info(
                f"Extracted {len(groups_list)} groups from '{self.config.groups_claim}' claim: {groups_list}",
                extra={"correlation_id": get_correlation_id()},
            )
        else:
            groups_list = []
            logger.info(
                f"No groups found in '{self.config.groups_claim}' claim or claim value is not a list",
                extra={"correlation_id": get_correlation_id()},
            )

        # Create OIDCUserInfo from response
        user_info = OIDCUserInfo(
            subject=data.get("sub", ""),
            email=email_value,
            email_verified=data.get("email_verified", False),
            username=username_value,
            groups=groups_list if groups_list else None,
        )

        return user_info
