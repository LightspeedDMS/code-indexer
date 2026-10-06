"""The one TOTP step-up that opens an elevation window.

REST ``POST /auth/elevate``, the Web elevation form and AJAX endpoints, and
MCP ``elevate_session`` all call :func:`step_up` once their own request
checks pass (enforcement switch, code presence, TOTP enrolment, session
key); each door keeps its own error shape.  The step-up owns everything
after that:

1. the attempt is reserved in the progressive throttle before any code is
   checked: while the key's backoff window runs the request is refused,
   even with a correct code, and produces no elevation outcome row.  There
   is no lock state, but the key is the username, so someone sending wrong
   codes for that account can keep its step-up throttled.  If the throttle
   store stays locked past its bound the outcome is ``BUSY`` (no code is
   checked; the door answers "try again shortly");
2. the code is verified: a recovery code opens a ``totp_repair`` window, a
   TOTP code a ``full`` one;
3. a wrong code counts toward the throttle (one limiter shared by every
   door, keyed by the username in its own step-up namespace) and records
   ``elevation_failed``;
4. a correct code opens the window, confirms it can be read back, clears the
   failure history and records ``elevation_granted``; once the window is
   readable, the row is ``elevation_granted`` even if clearing the failure
   history raises.

Every step-up that checks a code records exactly one outcome row;
``details`` carry the scope and whether a recovery code was used, never the
code.  Automatic elevation of credentialed requests (``auth/dependencies.py``)
opens windows directly through the session manager and records nothing:
only this step-up is an audited elevation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional

from code_indexer.server.services import audit_capture

if TYPE_CHECKING:
    from code_indexer.server.auth.elevated_session_manager import ElevatedSession
    from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter

SCOPE_FULL = "full"
SCOPE_TOTP_REPAIR = "totp_repair"

_GRANTED = "elevation_granted"
_FAILED = "elevation_failed"
_TARGET_TYPE = "user"


class StepUpOutcome(Enum):
    GRANTED = "granted"
    LOCKED_OUT = "locked_out"
    INVALID_CODE = "invalid_code"
    WINDOW_NOT_CREATED = "window_not_created"
    # The throttle store stayed locked past its bound; no code was checked.
    BUSY = "busy"


@dataclass(frozen=True)
class StepUpResult:
    """What the step-up did; ``session`` is set only when GRANTED."""

    outcome: StepUpOutcome
    scope: str
    used_recovery_code: bool
    session: Optional["ElevatedSession"] = None
    # Set when LOCKED_OUT: seconds until the throttle admits the next try.
    retry_after_seconds: float = 0.0

    def retry_after_header(self) -> Dict[str, str]:
        """``Retry-After`` for a refused step-up (whole seconds, >= 1)."""
        import math

        return {"Retry-After": str(max(1, math.ceil(self.retry_after_seconds)))}


def _record(actor: str, action_type: str, outcome: str, scope: str, used: bool) -> None:
    audit_capture.capture(
        actor=actor,
        action_type=action_type,
        target_type=_TARGET_TYPE,
        target_id=actor,
        outcome=outcome,
        details={"scope": scope, "used_recovery_code": used},
    )


def _verify(
    totp_service: Any,
    username: str,
    totp_code: Optional[str],
    recovery_code: Optional[str],
    client_ip: str,
) -> bool:
    if recovery_code:
        return bool(
            totp_service.verify_recovery_code(
                username, recovery_code, ip_address=client_ip
            )
        )
    return bool(totp_service.verify_enabled_code(username, totp_code))


def step_up(
    username: str,
    *,
    totp_code: Optional[str],
    recovery_code: Optional[str],
    session_key: str,
    client_ip: str,
    totp_service: Any,
    sessions: Any,
    limiter: "LoginRateLimiter",
) -> StepUpResult:
    """Verify one TOTP or recovery code and open *username*'s elevation window.

    A recovery code takes precedence when both are given.  *sessions* and
    *limiter* are the process-wide elevated-session manager and login rate
    limiter the calling door uses.  An exception from verification or from
    opening the window records ``elevation_failed`` and propagates.

    Raises:
        ValueError: blank *username* or *session_key*, or no code at all.
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("step_up requires a username")
    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError("step_up requires a session key")
    if not totp_code and not recovery_code:
        raise ValueError("step_up requires a TOTP code or a recovery code")

    used_recovery_code = bool(recovery_code)
    scope = SCOPE_TOTP_REPAIR if used_recovery_code else SCOPE_FULL
    # One budget per authenticated user: client addresses (behind the proxy,
    # the proxy's own) must not multiply it.  The step-up has its own
    # throttle namespace, so no login name can throttle (or be throttled
    # by) a step-up.
    limiter_key = username
    from code_indexer.server.auth.login_rate_limiter import (
        SCOPE_STEP_UP,
        ThrottleStoreBusy,
    )

    # Reserve the attempt before the code is checked (row-locked), so
    # concurrent step-ups cannot slip past the throttle.
    try:
        attempt = limiter.begin_attempt(limiter_key, scope=SCOPE_STEP_UP)
    except ThrottleStoreBusy:
        # The store stayed locked past its bound: no code is checked, the
        # door answers "try again shortly".
        return StepUpResult(
            StepUpOutcome.BUSY, scope, used_recovery_code, retry_after_seconds=1.0
        )
    if not attempt.admitted:
        return StepUpResult(
            StepUpOutcome.LOCKED_OUT,
            scope,
            used_recovery_code,
            retry_after_seconds=attempt.retry_after_seconds,
        )

    try:
        verified = _verify(totp_service, username, totp_code, recovery_code, client_ip)
        if not verified:
            _record(username, _FAILED, "failure", scope, used_recovery_code)
            return StepUpResult(StepUpOutcome.INVALID_CODE, scope, used_recovery_code)

        sessions.create(
            session_key=session_key,
            username=username,
            elevated_from_ip=client_ip,
            scope=scope,
        )
        # Clear the failure history only once the window is confirmed readable.
        session = sessions.get_status(session_key)
    except Exception:
        _record(username, _FAILED, "failure", scope, used_recovery_code)
        raise
    if session is None:
        _record(username, _FAILED, "failure", scope, used_recovery_code)
        return StepUpResult(StepUpOutcome.WINDOW_NOT_CREATED, scope, used_recovery_code)
    try:
        limiter.record_success(limiter_key, scope=SCOPE_STEP_UP)
    finally:
        # The window exists from here on: record it as granted even when
        # clearing the failure history raises (the error then propagates).
        _record(username, _GRANTED, "success", scope, used_recovery_code)
    return StepUpResult(StepUpOutcome.GRANTED, scope, used_recovery_code, session)
