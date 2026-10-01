"""
SQLite backend for OAuth 2.1 storage.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager


class OAuthSqliteBackend:
    """
    SQLite backend for OAuth 2.1 storage.

    Satisfies the OAuthBackend Protocol (protocols.py).
    Replicates OAuthManager data operations as a standalone backend
    using DatabaseConnectionManager for thread-safe atomic operations.
    """

    ACCESS_TOKEN_LIFETIME_HOURS = 8
    REFRESH_TOKEN_LIFETIME_DAYS = 30
    HARD_EXPIRATION_DAYS = 30
    EXTENSION_THRESHOLD_HOURS = 4

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        import secrets as _secrets
        import hashlib as _hashlib
        import base64 as _base64

        self._secrets = _secrets
        self._hashlib = _hashlib
        self._base64 = _base64
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create all OAuth tables if they do not already exist."""

        def _do_init(conn: sqlite3.Connection) -> None:
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
                    used INTEGER DEFAULT 0,
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
                INSERT OR IGNORE INTO oauth_clients
                    (client_id, client_name, redirect_uris, created_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "client_credentials",
                    "System: Client Credentials Grant",
                    "[]",
                    "2000-01-01T00:00:00+00:00",
                    "{}",
                ),
            )

        self._conn_manager.execute_atomic(_do_init)

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
        client_id = self._secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
            "scope": scope,
        }

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
                (
                    client_id,
                    client_name,
                    json.dumps(redirect_uris),
                    created_at,
                    json.dumps(metadata),
                ),
            )

        self._conn_manager.execute_atomic(_do_insert)
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
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,))
        row = cursor.fetchone()
        if row:
            return {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
                "redirect_uris": json.loads(row["redirect_uris"]),
                "created_at": row["created_at"],
            }
        return None

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

        code = self._secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO oauth_codes (code, client_id, user_id, code_challenge, redirect_uri, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    code,
                    client_id,
                    user_id,
                    code_challenge,
                    redirect_uri,
                    expires_at.isoformat(),
                ),
            )

        self._conn_manager.execute_atomic(_do_insert)
        return code

    def exchange_code_for_token(
        self, code: str, code_verifier: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a PKCE authorization code for access and refresh tokens."""
        from code_indexer.server.auth.oauth.oauth_manager import (
            OAuthError,
            PKCEVerificationError,
        )

        token_id = self._secrets.token_urlsafe(32)
        access_token = self._secrets.token_urlsafe(48)
        refresh_token = self._secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        hard_expires_at = now + timedelta(days=self.HARD_EXPIRATION_DAYS)

        result: Dict[str, Any] = {}

        def _do_exchange(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
            cursor.execute(
                "SELECT * FROM oauth_codes WHERE code = ? AND client_id = ?",
                (code, client_id),
            )
            code_row = cursor.fetchone()
            if not code_row:
                raise OAuthError("Invalid authorization code")
            if code_row["used"]:
                raise OAuthError("Authorization code already used")
            expires_at_dt = datetime.fromisoformat(code_row["expires_at"])
            if datetime.now(timezone.utc) > expires_at_dt:
                raise OAuthError("Authorization code expired")

            # PKCE verification
            code_challenge = code_row["code_challenge"]
            computed_challenge = (
                self._base64.urlsafe_b64encode(
                    self._hashlib.sha256(code_verifier.encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            if computed_challenge != code_challenge:
                raise PKCEVerificationError("PKCE verification failed")

            conn.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))

            token_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)

            conn.execute(
                """INSERT INTO oauth_tokens (token_id, client_id, user_id, access_token, refresh_token,
                   expires_at, created_at, last_activity, hard_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    code_row["client_id"],
                    code_row["user_id"],
                    access_token,
                    refresh_token,
                    token_expires_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    hard_expires_at.isoformat(),
                ),
            )
            result["access_token"] = access_token
            result["refresh_token"] = refresh_token

        self._conn_manager.execute_atomic(_do_exchange)

        return {
            "access_token": result["access_token"],
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
            "refresh_token": result["refresh_token"],
        }

    def validate_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Validate an access token and return its associated data."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM oauth_tokens WHERE access_token = ?", (access_token,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return None
        return {
            "token_id": row["token_id"],
            "client_id": row["client_id"],
            "user_id": row["user_id"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def extend_token_on_activity(self, access_token: str) -> bool:
        """Extend an access token's expiry if it is within the extension threshold."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM oauth_tokens WHERE access_token = ?", (access_token,)
        )
        row = cursor.fetchone()
        if not row:
            return False
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(row["expires_at"])
        hard_expires_at = datetime.fromisoformat(row["hard_expires_at"])
        remaining = (expires_at - now).total_seconds() / 3600
        if remaining >= self.EXTENSION_THRESHOLD_HOURS:
            return False
        new_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)
        if new_expires_at > hard_expires_at:
            new_expires_at = hard_expires_at

        def _do_extend(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE oauth_tokens SET expires_at = ?, last_activity = ? WHERE access_token = ?",
                (new_expires_at.isoformat(), now.isoformat(), access_token),
            )

        self._conn_manager.execute_atomic(_do_extend)
        return True

    def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a refresh token for new access and refresh tokens."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        new_access_token = self._secrets.token_urlsafe(48)
        new_refresh_token = self._secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)

        def _do_refresh(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
            cursor.execute(
                "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (refresh_token,)
            )
            row = cursor.fetchone()
            if not row:
                raise OAuthError("Invalid refresh token")

            conn.execute(
                """UPDATE oauth_tokens
                   SET access_token = ?, refresh_token = ?, expires_at = ?, last_activity = ?
                   WHERE refresh_token = ?""",
                (
                    new_access_token,
                    new_refresh_token,
                    new_expires_at.isoformat(),
                    now.isoformat(),
                    refresh_token,
                ),
            )

        self._conn_manager.execute_atomic(_do_refresh)

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

        def _do_revoke(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.row_factory = sqlite3.Row  # type: ignore[assignment]

            if token_type_hint == "access_token":
                cursor.execute(
                    "SELECT * FROM oauth_tokens WHERE access_token = ?", (token,)
                )
            elif token_type_hint == "refresh_token":
                cursor.execute(
                    "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (token,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM oauth_tokens WHERE access_token = ? OR refresh_token = ?",
                    (token, token),
                )

            row = cursor.fetchone()
            if not row:
                return

            token_id = row["token_id"]
            user_id = row["user_id"]
            access_token_val = row["access_token"]

            cursor.execute("DELETE FROM oauth_tokens WHERE token_id = ?", (token_id,))

            determined_type = (
                "access_token" if access_token_val == token else "refresh_token"
            )
            result["username"] = user_id
            result["token_type"] = determined_type

        self._conn_manager.execute_atomic(_do_revoke)

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

        token_id = self._secrets.token_urlsafe(32)
        access_token = self._secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)
        hard_expires_at = now + timedelta(days=self.HARD_EXPIRATION_DAYS)

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO oauth_tokens (token_id, client_id, user_id, access_token, refresh_token,
                   expires_at, created_at, last_activity, hard_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

        self._conn_manager.execute_atomic(_do_insert)

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

        def _do_link(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO oidc_identity_links (username, subject, email, linked_at, last_login)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, subject, email, now, now),
            )

        self._conn_manager.execute_atomic(_do_link)

    def get_oidc_identity(self, subject: str) -> Optional[Dict[str, Any]]:
        """Retrieve an OIDC identity link by subject."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM oidc_identity_links WHERE subject = ?", (subject,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "username": row["username"],
                "subject": row["subject"],
                "email": row["email"],
            }
        return None

    def delete_oidc_identity(self, subject: str) -> None:
        """Delete a stale OIDC identity link by subject."""

        def _do_delete(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM oidc_identity_links WHERE subject = ?", (subject,)
            )

        self._conn_manager.execute_atomic(_do_delete)

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass
