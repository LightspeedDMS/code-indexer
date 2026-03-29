"""
MFA Challenge Token Manager (Story #560).

Manages temporary challenge tokens that bind a password-verified user
to a TOTP challenge page. Tokens expire after 5 minutes and allow
a maximum of 5 verification attempts.
"""

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_CHALLENGE_TTL_SECONDS = 300  # 5 minutes
_MAX_ATTEMPTS = 5


@dataclass
class MfaChallenge:
    """A pending MFA challenge bound to a password-verified user."""

    username: str
    client_ip: str
    created_at: float
    attempt_count: int = 0
    redirect_url: str = "/admin/"


class MfaChallengeManager:
    """Manages MFA challenge tokens with TTL and attempt limits.

    Thread-safe. Tokens are stored in-memory (lost on restart, which
    is acceptable — user simply re-enters password).
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

    def create_challenge(
        self, username: str, client_ip: str, redirect_url: str = "/admin/"
    ) -> str:
        """Create a new challenge token for a password-verified user.

        Returns the opaque token string to embed in the challenge form.
        """
        token = secrets.token_urlsafe(32)
        challenge = MfaChallenge(
            username=username,
            client_ip=client_ip,
            created_at=time.time(),
            redirect_url=redirect_url,
        )
        with self._lock:
            self._cleanup_expired()
            self._challenges[token] = challenge
        logger.debug("MFA challenge created for %s", username)
        return token

    def get_challenge(self, token: str) -> Optional[MfaChallenge]:
        """Retrieve a valid (non-expired, not exhausted) challenge.

        Returns None if token is invalid, expired, or attempts exhausted.
        """
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
            return challenge

    def record_attempt(self, token: str) -> None:
        """Increment the attempt counter for a challenge."""
        with self._lock:
            challenge = self._challenges.get(token)
            if challenge is not None:
                challenge.attempt_count += 1

    def consume(self, token: str) -> Optional[MfaChallenge]:
        """Consume a challenge token (successful verification).

        Returns the challenge data and removes the token.
        """
        with self._lock:
            return self._challenges.pop(token, None)

    def _cleanup_expired(self) -> None:
        """Remove expired challenges. Called under lock."""
        now = time.time()
        expired = [
            t for t, c in self._challenges.items() if now - c.created_at > self._ttl
        ]
        for t in expired:
            del self._challenges[t]


# Module-level singleton
mfa_challenge_manager = MfaChallengeManager()
