"""Record the one outcome row of an audited state change.

Audited operations (account, credential and permission changes) call
:func:`record_outcome` exactly once per attempt: ``success`` after the
change, or ``failure`` when it raised or reported that nothing changed.
Recording never raises and never changes the operation's result (the
capture path is fail-open).

Invariant: only validated identifiers are stored.

- Callers pass as ``target_id`` only an identifier the operation itself
  verified: one it created, or one a lookup found persisted.  After a failed
  or unknown lookup they pass None.
- None, or a value that does not fit the target type's allowlisted id type,
  is replaced by a fixed placeholder (:data:`UNKNOWN_ACCOUNT_ACTOR` for
  accounts, :data:`UNRESOLVED_TARGET` otherwise).
- :func:`conforming_details` keeps only optional values that fit their
  allowlisted types.

So every stored identifier is either verified or a placeholder, and a
recorded outcome is never turned into a rejected (dropped) event.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Union

import anyio

from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AUDIT_TARGET_ID_TYPE,
    UNKNOWN_ACCOUNT_ACTOR,
    SystemComponent,
    conforms,
)

# Target id recorded for a non-account target the operation did not verify
# (a failed or unknown lookup, or a creation that persisted nothing).
UNRESOLVED_TARGET = "unresolved"

_ACCOUNT_TARGET_TYPES = frozenset({"user", "auth"})

AuditActor = Union[str, SystemComponent]


def audit_target_id(target_type: str, candidate: object) -> str:
    """Return *candidate* when it fits *target_type*'s id type, else a placeholder."""
    ftype = AUDIT_TARGET_ID_TYPE[target_type]
    if isinstance(candidate, str) and conforms(ftype, candidate):
        return candidate
    if target_type in _ACCOUNT_TARGET_TYPES:
        return UNKNOWN_ACCOUNT_ACTOR
    return UNRESOLVED_TARGET


def conforming_details(action_type: str, **candidates: Any) -> Dict[str, Any]:
    """Keep only the candidate values that fit the action's allowlisted types.

    ``None`` and non-conforming values are left out.  Naming a field the
    catalog does not allowlist for *action_type* is a programming defect and
    raises ``KeyError``.
    """
    schema = AUDIT_ACTION_CATALOG[action_type].details_schema or {}
    kept: Dict[str, Any] = {}
    for name, value in candidates.items():
        ftype = schema[name]
        if value is not None and conforms(ftype, value):
            kept[name] = value
    return kept


def record_outcome(
    *,
    actor: AuditActor,
    action_type: str,
    target_type: str,
    target_id: object,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """Record one outcome row (see module docstring); never raises."""
    safe_target = audit_target_id(target_type, target_id)
    if isinstance(actor, SystemComponent):
        audit_capture.capture_system(
            component=actor,
            action_type=action_type,
            target_type=target_type,
            target_id=safe_target,
            outcome=outcome,
            details=details,
        )
        return
    audit_capture.capture(
        actor=actor,
        action_type=action_type,
        target_type=target_type,
        target_id=safe_target,
        outcome=outcome,
        details=details,
    )


async def record_outcome_async(
    *,
    actor: AuditActor,
    action_type: str,
    target_type: str,
    target_id: object,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """:func:`record_outcome` from ``async def`` code, off the event loop."""

    def _record() -> None:
        record_outcome(
            actor=actor,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
        )

    await anyio.to_thread.run_sync(_record)
