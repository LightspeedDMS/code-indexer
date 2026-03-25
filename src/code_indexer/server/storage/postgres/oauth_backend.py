"""
PostgreSQL backend for OAuth 2.1 storage (Story #514).

Drop-in replacement for OAuthSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the OAuthBackend Protocol (protocols.py).

Tables created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required for these tables.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class OAuthPostgresBackend:
    """
    PostgreSQL backend for OAuth 2.1 storage.

    Satisfies the OAuthBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).
    """

    ACCESS_TOKEN_LIFETIME_HOURS = 8
    REFRESH_TOKEN_LIFETIME_DAYS = 30
    HARD_EXPIRATION_DAYS = 30
    EXTENSION_THRESHOLD_HOURS = 4

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure tables exist.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create all OAuth tables if they do not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_clients (
                        client_id TEXT PRIMARY KEY,
                        client_name TEXT NOT NULL,
                        redirect_uris TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        metadata TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_codes (
                        code TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        code_challenge TEXT NOT NULL,
                        redirect_uri TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used BOOLEAN DEFAULT FALSE,
                        FOREIGN KEY (client_id) REFERENCES oauth_clients (client_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_tokens (
                        token_id TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        access_token TEXT UNIQUE NOT NULL,
                        refresh_token TEXT UNIQUE,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_activity TEXT NOT NULL,
                        hard_expires_at TEXT NOT NULL,
                        FOREIGN KEY (client_id) REFERENCES oauth_clients (client_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tokens_access ON oauth_tokens (access_token)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oidc_identity_links (
                        username TEXT NOT NULL PRIMARY KEY,
                        subject TEXT NOT NULL UNIQUE,
                        email TEXT,
                        linked_at TEXT NOT NULL,
                        last_login TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_oidc_subject ON oidc_identity_links (subject)"
                )
                # Seed synthetic client_credentials row to satisfy FK constraint.
                conn.execute(
                    """
                    INSERT INTO oauth_clients
                        (client_id, client_name, redirect_uris, created_at, metadata)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (client_id) DO NOTHING
                    """,
                    (
                        "client_credentials",
                        "System: Client Credentials Grant",
                        "[]",
                        "2000-01-01T00:00:00+00:00",
                        "{}",
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: schema setup failed: %s", exc)

    def register_client(
        self,
        client_name: str,
        redirect_uris: List[str],
        grant_types: Optional[List[str]] = None,
        response_types: Optional[List[str]] = None,
        token_endpoint_auth_method: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a new OAuth client and return its registration data."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        if not client_name or client_name.strip() == "":
            raise OAuthError("client_name cannot be empty")
        client_id = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
            "scope": scope,
        }

        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at, metadata) VALUES (%s, %s, %s, %s, %s)",
                    (
                        client_id,
                        client_name,
                        json.dumps(redirect_uris),
                        created_at,
                        json.dumps(metadata),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: register_client failed: %s", exc)
            raise

        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "client_secret_expires_at": 0,
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
        }

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a registered client by its client_id."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT client_id, client_name, redirect_uris, created_at FROM oauth_clients WHERE client_id = %s",
                    (client_id,),
                ).fetchone()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: get_client failed: %s", exc)
            return None

        if row is None:
            return None
        return {
            "client_id": row[0],
            "client_name": row[1],
            "redirect_uris": json.loads(row[2]),
            "created_at": row[3],
        }

    def generate_authorization_code(
        self,
        client_id: str,
        user_id: str,
        code_challenge: str,
        redirect_uri: str,
        state: str,
    ) -> str:
        """Generate a one-time PKCE authorization code."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        if not code_challenge or code_challenge.strip() == "":
            raise OAuthError("code_challenge required")

        client = self.get_client(client_id)
        if not client:
            raise OAuthError(f"Invalid client_id: {client_id}")
        if redirect_uri not in client["redirect_uris"]:
            raise OAuthError(f"Invalid redirect_uri: {redirect_uri}")

        code = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO oauth_codes (code, client_id, user_id, code_challenge, redirect_uri, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        code,
                        client_id,
                        user_id,
                        code_challenge,
                        redirect_uri,
                        expires_at.isoformat(),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "OAuthPostgresBackend: generate_authorization_code failed: %s", exc
            )
            raise

        return code

    def exchange_code_for_token(
        self, code: str, code_verifier: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a PKCE authorization code for access and refresh tokens."""
        from code_indexer.server.auth.oauth.oauth_manager import (
            OAuthError,
            PKCEVerificationError,
        )

        token_id = secrets.token_urlsafe(32)
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        hard_expires_at = now + timedelta(days=self.HARD_EXPIRATION_DAYS)

        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT code, client_id, user_id, code_challenge, expires_at, used FROM oauth_codes WHERE code = %s AND client_id = %s",
                    (code, client_id),
                ).fetchone()

                if not row:
                    raise OAuthError("Invalid authorization code")
                used_val = row[5]
                if used_val:
                    raise OAuthError("Authorization code already used")
                expires_at_dt = datetime.fromisoformat(row[4])
                if datetime.now(timezone.utc) > expires_at_dt:
                    raise OAuthError("Authorization code expired")

                # PKCE verification
                stored_challenge = row[3]
                computed_challenge = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(code_verifier.encode()).digest()
                    )
                    .decode()
                    .rstrip("=")
                )
                if computed_challenge != stored_challenge:
                    raise PKCEVerificationError("PKCE verification failed")

                conn.execute(
                    "UPDATE oauth_codes SET used = TRUE WHERE code = %s", (code,)
                )

                token_expires_at = now + timedelta(
                    hours=self.ACCESS_TOKEN_LIFETIME_HOURS
                )
                code_client_id = row[1]
                code_user_id = row[2]

                conn.execute(
                    """INSERT INTO oauth_tokens (token_id, client_id, user_id, access_token, refresh_token,
                       expires_at, created_at, last_activity, hard_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        token_id,
                        code_client_id,
                        code_user_id,
                        access_token,
                        refresh_token,
                        token_expires_at.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        hard_expires_at.isoformat(),
                    ),
                )
                conn.commit()
        except (OAuthError, PKCEVerificationError):
            raise
        except Exception as exc:
            logger.warning(
                "OAuthPostgresBackend: exchange_code_for_token failed: %s", exc
            )
            raise

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
            "refresh_token": refresh_token,
        }

    def validate_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Validate an access token and return its associated data."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT token_id, client_id, user_id, expires_at, created_at FROM oauth_tokens WHERE access_token = %s",
                    (access_token,),
                ).fetchone()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: validate_token failed: %s", exc)
            return None

        if row is None:
            return None
        expires_at = datetime.fromisoformat(row[3])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            return None
        return {
            "token_id": row[0],
            "client_id": row[1],
            "user_id": row[2],
            "expires_at": row[3],
            "created_at": row[4],
        }

    def extend_token_on_activity(self, access_token: str) -> bool:
        """Extend an access token's expiry if it is within the extension threshold."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT expires_at, hard_expires_at FROM oauth_tokens WHERE access_token = %s",
                    (access_token,),
                ).fetchone()
                if row is None:
                    return False

                now = datetime.now(timezone.utc)
                expires_at = datetime.fromisoformat(row[0])
                hard_expires_at = datetime.fromisoformat(row[1])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if hard_expires_at.tzinfo is None:
                    hard_expires_at = hard_expires_at.replace(tzinfo=timezone.utc)

                remaining = (expires_at - now).total_seconds() / 3600
                if remaining >= self.EXTENSION_THRESHOLD_HOURS:
                    return False

                new_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)
                if new_expires_at > hard_expires_at:
                    new_expires_at = hard_expires_at

                conn.execute(
                    "UPDATE oauth_tokens SET expires_at = %s, last_activity = %s WHERE access_token = %s",
                    (new_expires_at.isoformat(), now.isoformat(), access_token),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "OAuthPostgresBackend: extend_token_on_activity failed: %s", exc
            )
            return False

        return True

    def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a refresh token for new access and refresh tokens."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        new_access_token = secrets.token_urlsafe(48)
        new_refresh_token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)

        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT token_id FROM oauth_tokens WHERE refresh_token = %s",
                    (refresh_token,),
                ).fetchone()
                if not row:
                    raise OAuthError("Invalid refresh token")

                conn.execute(
                    """UPDATE oauth_tokens
                       SET access_token = %s, refresh_token = %s, expires_at = %s, last_activity = %s
                       WHERE refresh_token = %s""",
                    (
                        new_access_token,
                        new_refresh_token,
                        new_expires_at.isoformat(),
                        now.isoformat(),
                        refresh_token,
                    ),
                )
                conn.commit()
        except OAuthError:
            raise
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: refresh_access_token failed: %s", exc)
            raise

        return {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
            "refresh_token": new_refresh_token,
        }

    def revoke_token(
        self, token: str, token_type_hint: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        """Revoke an access or refresh token."""
        result: Dict[str, Optional[str]] = {"username": None, "token_type": None}

        try:
            with self._pool.connection() as conn:
                if token_type_hint == "access_token":
                    row = conn.execute(
                        "SELECT token_id, user_id, access_token FROM oauth_tokens WHERE access_token = %s",
                        (token,),
                    ).fetchone()
                elif token_type_hint == "refresh_token":
                    row = conn.execute(
                        "SELECT token_id, user_id, access_token FROM oauth_tokens WHERE refresh_token = %s",
                        (token,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT token_id, user_id, access_token FROM oauth_tokens WHERE access_token = %s OR refresh_token = %s",
                        (token, token),
                    ).fetchone()

                if row is None:
                    return result

                token_id = row[0]
                user_id = row[1]
                access_token_val = row[2]

                conn.execute(
                    "DELETE FROM oauth_tokens WHERE token_id = %s", (token_id,)
                )
                conn.commit()

                determined_type = (
                    "access_token" if access_token_val == token else "refresh_token"
                )
                result["username"] = user_id
                result["token_type"] = determined_type
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: revoke_token failed: %s", exc)

        return result

    def handle_client_credentials_grant(
        self,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        mcp_credential_manager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Handle OAuth 2.1 client_credentials grant type."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        if not client_id or not client_secret:
            raise OAuthError("client_id and client_secret required")

        if not mcp_credential_manager:
            raise OAuthError("MCPCredentialManager not available")

        user_id = mcp_credential_manager.verify_credential(client_id, client_secret)
        if not user_id:
            raise OAuthError("Invalid client credentials")

        token_id = secrets.token_urlsafe(32)
        access_token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)
        hard_expires_at = now + timedelta(days=self.HARD_EXPIRATION_DAYS)

        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """INSERT INTO oauth_tokens (token_id, client_id, user_id, access_token, refresh_token,
                       expires_at, created_at, last_activity, hard_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        token_id,
                        "client_credentials",
                        user_id,
                        access_token,
                        None,
                        expires_at.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        hard_expires_at.isoformat(),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "OAuthPostgresBackend: handle_client_credentials_grant failed: %s", exc
            )
            raise

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
        }

    def link_oidc_identity(
        self, username: str, subject: str, email: Optional[str] = None
    ) -> None:
        """Link an OIDC subject to a local username."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO oidc_identity_links (username, subject, email, linked_at, last_login)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username) DO UPDATE SET
                        subject = EXCLUDED.subject,
                        email = EXCLUDED.email,
                        last_login = EXCLUDED.last_login
                    """,
                    (username, subject, email, now, now),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: link_oidc_identity failed: %s", exc)

    def get_oidc_identity(self, subject: str) -> Optional[Dict[str, Any]]:
        """Retrieve an OIDC identity link by subject."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT username, subject, email FROM oidc_identity_links WHERE subject = %s",
                    (subject,),
                ).fetchone()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: get_oidc_identity failed: %s", exc)
            return None

        if row is None:
            return None
        return {
            "username": row[0],
            "subject": row[1],
            "email": row[2],
        }

    def delete_oidc_identity(self, subject: str) -> None:
        """Delete a stale OIDC identity link by subject."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM oidc_identity_links WHERE subject = %s", (subject,)
                )
                conn.commit()
        except Exception as exc:
            logger.warning("OAuthPostgresBackend: delete_oidc_identity failed: %s", exc)

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
