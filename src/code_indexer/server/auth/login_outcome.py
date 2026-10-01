"""Login outcome entry point: exactly one audit row per login attempt.

Every login door calls :func:`complete_login` when it issues a token or
session and :func:`reject_login` when it refuses the attempt.  A password
step that only returns an MFA challenge calls neither: the attempt's outcome
is recorded when the challenge is answered.

Rows go through the unified capture path (``audit_capture.capture``), which
never raises: an audit failure is counted and logged, and the login result
is unchanged.

``details`` carries only the allowlisted enum fields ``method``, ``mfa``,
``flow``, ``stage`` and ``reason``.  A refused attempt is attributed to the
typed name only when it names an EXISTING account (the caller says so);
otherwise to the fixed ``(unknown)`` placeholder, so text typed into the
wrong field -- a password, for example -- is never stored.
"""

from __future__ import annotations

from typing import Callable, Dict, TypeVar

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import (
    UNKNOWN_ACCOUNT_ACTOR,
    USERNAME,
    conforms,
)

LOGIN_SUCCESS = "authentication_success"
LOGIN_FAILURE = "authentication_failure"
_LOGIN_TARGET_TYPE = "auth"
_PRE_AUTHENTICATION = "none"

# How the caller will authenticate with what a successful login issued.
_FLOW_AUTH_METHOD: Dict[str, str] = {
    "rest_token": "jwt",
    "mcp_jwt": "jwt",
    "web_session": "web_session",
    "oauth_code": "oauth_token",
}

T = TypeVar("T")


def login_actor(attempted_username: object, *, account_exists: bool) -> str:
    """The actor recorded for a refused attempt (see module docstring)."""
    if (
        account_exists
        and isinstance(attempted_username, str)
        and conforms(USERNAME, attempted_username)
    ):
        return attempted_username
    return UNKNOWN_ACCOUNT_ACTOR


def reject_login(
    attempted_username: object,
    *,
    account_exists: bool,
    method: str,
    stage: str,
    reason: str,
) -> None:
    """Record one refused login attempt (never raises).

    *account_exists* must come from the door's own account lookup; only
    then is the typed name recorded.
    """
    actor = login_actor(attempted_username, account_exists=account_exists)
    audit_capture.capture(
        actor=actor,
        action_type=LOGIN_FAILURE,
        target_type=_LOGIN_TARGET_TYPE,
        target_id=actor,
        outcome="failure",
        details={"method": method, "stage": stage, "reason": reason},
        auth_method=_PRE_AUTHENTICATION,
    )


def complete_login(
    username: str, *, method: str, mfa: str, flow: str, issue: Callable[[], T]
) -> T:
    """Issue the login's token or session and record its one outcome row.

    *issue* performs the issuance.  If it raises, the attempt is recorded as
    refused at the ``issuance`` stage and the exception propagates; otherwise
    one success row is recorded and the issuance result is returned.
    """
    try:
        issued = issue()
    except Exception:
        reject_login(
            username,
            account_exists=True,  # issuance follows a successful authentication
            method=method,
            stage="issuance",
            reason="server_error",
        )
        raise
    audit_capture.capture(
        actor=username,
        action_type=LOGIN_SUCCESS,
        target_type=_LOGIN_TARGET_TYPE,
        target_id=username,
        outcome="success",
        details={"method": method, "mfa": mfa, "flow": flow},
        auth_method=_FLOW_AUTH_METHOD[flow],
    )
    return issued
