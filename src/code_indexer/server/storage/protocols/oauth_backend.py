"""OAuthBackend Protocol.

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class OAuthBackend(Protocol):
    """
    Protocol for OAuth 2.1 token and client storage.

    Satisfies PEP 544 structural subtyping: any class implementing all
    of these methods is accepted as an OAuthBackend without inheritance.
    """

    def register_client(
        self,
        client_name: str,
        redirect_uris: List[str],
        grant_types: Optional[List[str]] = None,
        response_types: Optional[List[str]] = None,
        token_endpoint_auth_method: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a new OAuth client and return its registration data.

        Args:
            client_name: Human-readable name for the client.
            redirect_uris: List of allowed redirect URIs.
            grant_types: Allowed grant types (default: authorization_code, refresh_token).
            response_types: Allowed response types (default: code).
            token_endpoint_auth_method: Auth method (default: none).
            scope: Optional scope string.

        Returns:
            Dict with client_id and registration metadata.
        """
        ...

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a registered client by its client_id.

        Args:
            client_id: The client identifier.

        Returns:
            Dict with client data, or None if not found.
        """
        ...

    def generate_authorization_code(
        self,
        client_id: str,
        user_id: str,
        code_challenge: str,
        redirect_uri: str,
        state: Optional[str] = None,
    ) -> str:
        """Generate a one-time PKCE authorization code.

        Args:
            client_id: The registered OAuth client ID.
            user_id: The authenticated user identifier.
            code_challenge: PKCE S256 code challenge.
            redirect_uri: The redirect URI for this request.
            state: Opaque state value from the authorization request.

        Returns:
            A short-lived authorization code string.
        """
        ...

    def exchange_code_for_token(
        self, code: str, code_verifier: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a PKCE authorization code for access and refresh tokens.

        Args:
            code: The authorization code to exchange.
            code_verifier: PKCE code verifier for S256 verification.
            client_id: The OAuth client requesting the exchange.

        Returns:
            Dict with access_token, token_type, expires_in, refresh_token.
        """
        ...

    def validate_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Validate an access token and return its associated data.

        Args:
            access_token: The bearer token to validate.

        Returns:
            Dict with token_id, client_id, user_id, expires_at, created_at —
            or None if the token is unknown or expired.
        """
        ...

    def extend_token_on_activity(self, access_token: str) -> bool:
        """Extend an access token's expiry if it is within the extension threshold.

        Args:
            access_token: The bearer token to potentially extend.

        Returns:
            True if the token was extended, False otherwise.
        """
        ...

    def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a refresh token for new access and refresh tokens.

        Args:
            refresh_token: The refresh token to exchange.
            client_id: The OAuth client requesting the refresh.

        Returns:
            Dict with access_token, token_type, expires_in, refresh_token.
        """
        ...

    def revoke_token(
        self, token: str, token_type_hint: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        """Revoke an access or refresh token.

        Args:
            token: The token to revoke.
            token_type_hint: Optional hint ('access_token' or 'refresh_token').

        Returns:
            Dict with username and token_type if found, None values otherwise.
        """
        ...

    def handle_client_credentials_grant(
        self,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        mcp_credential_manager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Handle OAuth 2.1 client_credentials grant type.

        Args:
            client_id: MCP client ID.
            client_secret: MCP client secret.
            scope: Optional scope string.
            mcp_credential_manager: MCPCredentialManager for credential verification.

        Returns:
            Dict with access_token, token_type, expires_in.
        """
        ...

    def link_oidc_identity(
        self, username: str, subject: str, email: Optional[str] = None
    ) -> None:
        """Link an OIDC subject to a local username.

        Args:
            username: Local CIDX username.
            subject: OIDC subject identifier (sub claim).
            email: Optional email address from OIDC provider.
        """
        ...

    def get_oidc_identity(self, subject: str) -> Optional[Dict[str, Any]]:
        """Retrieve an OIDC identity link by subject.

        Args:
            subject: OIDC subject identifier (sub claim).

        Returns:
            Dict with username, subject, email — or None if not found.
        """
        ...

    def delete_oidc_identity(self, subject: str) -> None:
        """Delete a stale OIDC identity link by subject.

        Args:
            subject: OIDC subject identifier (sub claim) to delete.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
