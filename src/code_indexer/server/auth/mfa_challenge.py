"""
MFA Challenge Token Manager (Story #560, C3 cluster support).

Manages temporary challenge tokens that bind a password-verified user
to a TOTP challenge page. Tokens expire after 5 minutes and allow
a maximum of 5 verification attempts.

Dual-mode: in-memory (standalone) or PostgreSQL (cluster).
"""

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    from psycopg.rows import dict_row
except ImportError:  # psycopg3 not installed (standalone mode)
    dict_row = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_CHALLENGE_TTL_SECONDS = 300  # 5 minutes
_MAX_ATTEMPTS = 5


@dataclass
class MfaChallenge:
    """A pending MFA challenge bound to a password-verified user."""

    username: str
    role: str
    client_ip: str
    created_at: float
    attempt_count: int = 0
    redirect_url: str = "/admin/"
    # OAuth context fields (Story #562): stored when MFA is triggered
    # during the OAuth authorization flow so the auth code can be
    # generated after TOTP verification.
    oauth_client_id: Optional[str] = None
    oauth_redirect_uri: Optional[str] = None
    oauth_code_challenge: Optional[str] = None
    oauth_state: Optional[str] = None


class MfaChallengeManager:
    """Manages MFA challenge tokens with TTL and attempt limits.

    Thread-safe. Standalone mode stores tokens in-memory (lost on
    restart, which is acceptable -- user simply re-enters password).
    Cluster mode stores tokens in PostgreSQL via connection pool.
    """

    def __init__(
        self,
        ttl_seconds: int = _CHALLENGE_TTL_SECONDS,
        max_attempts: int = _MAX_ATTEMPTS,
    ):
        self._challenges: Dict[str, MfaChallenge] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._pool: Optional[Any] = None

    def set_connection_pool(self, pool: Any) -> None:
        """Set PostgreSQL connection pool for cluster mode.

        When set, all challenge operations use PostgreSQL instead of
        the in-memory dict, enabling cross-node MFA verification.
        """
        self._pool = pool
        logger.info(
            "MfaChallengeManager: using PostgreSQL connection pool (cluster mode)"
        )

    # ------------------------------------------------------------------
    # PostgreSQL helpers
    # ------------------------------------------------------------------

    def _row_to_challenge(self, row: Any) -> MfaChallenge:
        """Convert a database row to an MfaChallenge dataclass."""
        return MfaChallenge(
            username=row["username"],
            role=row["role"],
            client_ip=row["client_ip"],
            created_at=float(row["created_at"]),
            attempt_count=int(row["attempt_count"]),
            redirect_url=row["redirect_url"] or "/admin/",
            oauth_client_id=row["oauth_client_id"],
            oauth_redirect_uri=row["oauth_redirect_uri"],
            oauth_code_challenge=row["oauth_code_challenge"],
            oauth_state=row["oauth_state"],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_challenge(
        self,
        username: str,
        role: str,
        client_ip: str,
        redirect_url: str = "/admin/",
        oauth_client_id: Optional[str] = None,
        oauth_redirect_uri: Optional[str] = None,
        oauth_code_challenge: Optional[str] = None,
        oauth_state: Optional[str] = None,
    ) -> str:
        """Create a new challenge token for a password-verified user.

        Returns the opaque token string to embed in the challenge form.
        OAuth parameters are stored when the challenge originates from
        an OAuth authorization flow (Story #562).
        """
        token = secrets.token_urlsafe(32)
        now = time.time()

        if self._pool is not None:
            self._cleanup_expired()
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO mfa_challenges "
                    "(token, username, role, client_ip, redirect_url, created_at, "
                    "attempt_count, oauth_client_id, oauth_redirect_uri, "
                    "oauth_code_challenge, oauth_state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        token,
                        username,
                        role,
                        client_ip,
                        redirect_url,
                        now,
                        0,
                        oauth_client_id,
                        oauth_redirect_uri,
                        oauth_code_challenge,
                        oauth_state,
                    ),
                )
                conn.commit()
        else:
            challenge = MfaChallenge(
                username=username,
                role=role,
                client_ip=client_ip,
                created_at=now,
                redirect_url=redirect_url,
                oauth_client_id=oauth_client_id,
                oauth_redirect_uri=oauth_redirect_uri,
                oauth_code_challenge=oauth_code_challenge,
                oauth_state=oauth_state,
            )
            with self._lock:
                self._cleanup_expired()
                self._challenges[token] = challenge

        logger.debug("MFA challenge created for %s", username)
        return token

    def get_challenge(
        self, token: str, client_ip: Optional[str] = None
    ) -> Optional[MfaChallenge]:
        """Retrieve a valid (non-expired, not exhausted) challenge.

        Args:
            token: The challenge token.
            client_ip: If provided, validates that the request IP matches
                the IP from challenge creation. Rejects on mismatch.

        Returns None if token is invalid, expired, exhausted, or IP mismatched.
        """
        if self._pool is not None:
            return self._get_challenge_pg(token, client_ip)
        return self._get_challenge_mem(token, client_ip)

    def _get_challenge_pg(
        self, token: str, client_ip: Optional[str]
    ) -> Optional[MfaChallenge]:
        """Retrieve challenge from PostgreSQL."""
        assert self._pool is not None
        cutoff = time.time() - self._ttl
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT * FROM mfa_challenges WHERE token = %s AND created_at > %s",
                (token, cutoff),
            ).fetchone()
            if row is None:
                # Clean up if it existed but expired
                conn.execute(
                    "DELETE FROM mfa_challenges WHERE token = %s AND created_at <= %s",
                    (token, cutoff),
                )
                conn.commit()
                return None

            challenge = self._row_to_challenge(row)

            if challenge.attempt_count >= self._max_attempts:
                conn.execute("DELETE FROM mfa_challenges WHERE token = %s", (token,))
                conn.commit()
                logger.warning(
                    "MFA challenge exhausted for %s (max attempts reached)",
                    challenge.username,
                )
                return None

            if client_ip and challenge.client_ip != client_ip:
                logger.warning(
                    "MFA challenge IP mismatch for %s: expected %s got %s",
                    challenge.username,
                    challenge.client_ip,
                    client_ip,
                )
                return None

            return challenge

    def _get_challenge_mem(
        self, token: str, client_ip: Optional[str]
    ) -> Optional[MfaChallenge]:
        """Retrieve challenge from in-memory dict."""
        with self._lock:
            challenge = self._challenges.get(token)
            if challenge is None:
                return None
            if time.time() - challenge.created_at > self._ttl:
                del self._challenges[token]
                return None
            if challenge.attempt_count >= self._max_attempts:
                del self._challenges[token]
                logger.warning(
                    "MFA challenge exhausted for %s (max attempts reached)",
                    challenge.username,
                )
                return None
            if client_ip and challenge.client_ip != client_ip:
                logger.warning(
                    "MFA challenge IP mismatch for %s: expected %s got %s",
                    challenge.username,
                    challenge.client_ip,
                    client_ip,
                )
                return None
            return challenge

    def record_attempt(self, token: str) -> None:
        """Increment the attempt counter for a challenge."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE mfa_challenges "
                    "SET attempt_count = attempt_count + 1 "
                    "WHERE token = %s",
                    (token,),
                )
                conn.commit()
        else:
            with self._lock:
                challenge = self._challenges.get(token)
                if challenge is not None:
                    challenge.attempt_count += 1

    def consume(self, token: str) -> Optional[MfaChallenge]:
        """Consume a challenge token (successful verification).

        Returns the challenge data and removes the token.
        Returns None if token is invalid or expired (TTL enforced).
        """
        if self._pool is not None:
            return self._consume_pg(token)
        return self._consume_mem(token)

    def _consume_pg(self, token: str) -> Optional[MfaChallenge]:
        """Consume challenge from PostgreSQL."""
        assert self._pool is not None
        cutoff = time.time() - self._ttl
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT * FROM mfa_challenges WHERE token = %s AND created_at > %s",
                (token, cutoff),
            ).fetchone()
            if row is None:
                # Clean up expired entry if present
                conn.execute("DELETE FROM mfa_challenges WHERE token = %s", (token,))
                conn.commit()
                return None
            challenge = self._row_to_challenge(row)
            conn.execute("DELETE FROM mfa_challenges WHERE token = %s", (token,))
            conn.commit()
            return challenge

    def _consume_mem(self, token: str) -> Optional[MfaChallenge]:
        """Consume challenge from in-memory dict."""
        with self._lock:
            challenge = self._challenges.get(token)
            if challenge is None:
                return None
            if time.time() - challenge.created_at > self._ttl:
                del self._challenges[token]
                return None
            return self._challenges.pop(token)

    def _cleanup_expired(self) -> None:
        """Remove expired challenges."""
        if self._pool is not None:
            cutoff = time.time() - self._ttl
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM mfa_challenges WHERE created_at <= %s",
                    (cutoff,),
                )
                conn.commit()
        else:
            now = time.time()
            expired = [
                t for t, c in self._challenges.items() if now - c.created_at > self._ttl
            ]
            for t in expired:
                del self._challenges[t]


# Singleton instance. In standalone mode this is process-local in-memory.
# In cluster mode, set_connection_pool() switches to PostgreSQL storage.
mfa_challenge_manager = MfaChallengeManager()
