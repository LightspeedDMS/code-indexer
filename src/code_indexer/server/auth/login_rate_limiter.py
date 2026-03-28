"""
Login rate limiter for account lockout after repeated failed login attempts.

Story #557: Implements per-username account lockout with sliding window tracking.
Complements the token-bucket rate limiter (Story #555) which handles burst limiting.
This class handles sustained-failure lockout: >= 5 failures in 15 min window.

Thread-safe with threading.Lock. Zero fallbacks - fails fast on bad state.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Tuple


class LoginRateLimiter:
    """
    Per-username login lockout based on failed attempt count in a sliding window.

    Security requirements (Story #557):
    - Sliding window: only failures within window_minutes count
    - Lockout after max_attempts failures within the window
    - Lockout duration: lockout_duration_minutes
    - Success clears failure history
    - Configurable enable/disable toggle
    - Audit logging for each failure and lockout event
    """

    def __init__(
        self,
        max_attempts: int = 5,
        lockout_duration_minutes: float = 15,
        window_minutes: float = 15,
        enabled: bool = True,
        audit_logger=None,
    ) -> None:
        """
        Initialize the login rate limiter.

        Args:
            max_attempts: Number of failures before lockout (default 5)
            lockout_duration_minutes: How long the lockout lasts (default 15)
            window_minutes: Sliding window for counting failures (default 15)
            enabled: If False, all checks are no-ops (AC6)
            audit_logger: Optional PasswordChangeAuditLogger for audit events (AC7)
        """
        self._max_attempts = max_attempts
        self._lockout_duration_seconds = lockout_duration_minutes * 60.0
        self._window_seconds = window_minutes * 60.0
        self._enabled = enabled
        self._audit_logger = audit_logger
        self._lock = threading.Lock()
        # Per-username list of failure timestamps (monotonic seconds)
        self._failures: Dict[str, List[float]] = {}
        # Per-username lockout expiry timestamp (monotonic seconds), None if not locked
        self._lockout_until: Dict[str, float] = {}

    def is_locked(self, username: str) -> Tuple[bool, float]:
        """
        Check if the account is currently locked out.

        Args:
            username: The username to check

        Returns:
            (is_locked, remaining_seconds) - remaining_seconds is 0 when not locked
        """
        if not self._enabled:
            return False, 0

        with self._lock:
            return self._check_locked(username)

    def check_and_record_failure(self, username: str) -> Tuple[bool, float]:
        """
        Record a failed login attempt and check if the account is now locked.

        Audit-logs the failure (AC7). If this failure triggers lockout,
        also audit-logs the lockout event.

        Args:
            username: The username that failed authentication

        Returns:
            (is_locked, remaining_seconds) - True if account is now locked
        """
        if not self._enabled:
            return False, 0

        with self._lock:
            now = time.monotonic()

            # If currently locked, report locked state without adding another failure
            locked, remaining = self._check_locked(username)
            if locked:
                self._emit_failure_audit(username)
                return True, remaining

            # Prune failures outside the sliding window
            self._prune_old_failures(username, now)

            # Record this failure
            if username not in self._failures:
                self._failures[username] = []
            self._failures[username].append(now)

            # Audit-log this individual failure (AC7)
            self._emit_failure_audit(username)

            # Check if we've hit the lockout threshold
            failure_count = len(self._failures[username])
            if failure_count >= self._max_attempts:
                lockout_until = now + self._lockout_duration_seconds
                self._lockout_until[username] = lockout_until
                remaining = self._lockout_duration_seconds
                # Audit-log the lockout event (AC7)
                self._emit_lockout_audit(username, failure_count)
                return True, remaining

            return False, 0

    def record_success(self, username: str) -> None:
        """
        Record a successful login, clearing all failure history for the username.

        AC1: Successful login resets failure counter to 0.

        Args:
            username: The username that authenticated successfully
        """
        if not self._enabled:
            return

        with self._lock:
            self._failures.pop(username, None)
            self._lockout_until.pop(username, None)

    # ------------------------------------------------------------------
    # Internal helpers (called under lock)
    # ------------------------------------------------------------------

    def _check_locked(self, username: str) -> Tuple[bool, float]:
        """Return (is_locked, remaining_seconds). Must be called under self._lock."""
        lockout_until = self._lockout_until.get(username)
        if lockout_until is None:
            return False, 0
        now = time.monotonic()
        if now < lockout_until:
            return True, lockout_until - now
        # Lockout expired - clean up
        del self._lockout_until[username]
        self._failures.pop(username, None)
        return False, 0

    def _prune_old_failures(self, username: str, now: float) -> None:
        """Remove failure timestamps older than the sliding window."""
        cutoff = now - self._window_seconds
        if username in self._failures:
            self._failures[username] = [
                ts for ts in self._failures[username] if ts > cutoff
            ]

    def _emit_failure_audit(self, username: str) -> None:
        """Emit audit log entry for a failed login attempt."""
        if self._audit_logger is None:
            return
        self._audit_logger.log_authentication_failure(
            username=username,
            error_type="login_failed",
            message=f"Failed login attempt for user: {username}",
        )

    def _emit_lockout_audit(self, username: str, attempt_count: int) -> None:
        """Emit audit log entry for account lockout."""
        if self._audit_logger is None:
            return
        self._audit_logger.log_rate_limit_triggered(
            username=username,
            ip_address="unknown",
            attempt_count=attempt_count,
        )


# Module-level singleton used by inline_auth and web/routes
login_rate_limiter = LoginRateLimiter()
