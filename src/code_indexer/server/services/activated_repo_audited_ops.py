"""Audited entry points for an admin managing ANOTHER user's activated repos.

The admin doors that activate a golden repository for a user, or remove a
user's activated repository, reach the change through these functions,
which take the acting admin as a required keyword argument and record
exactly one job-based row (a row means "submitted", never "completed"):
``success`` once the job is submitted, or ``failure`` when the request is
refused, after which the original exception propagates.

- ``target_id`` is the user the job was submitted for; ``details`` carry the
  user's alias for the copy, the golden alias (activation) and the job id.
  A refused request verified nothing, so it records the account placeholder
  and no details.
- A caller acting on their OWN copy writes no row: the catalog has no
  self-service activation type, so the self-service doors (and an admin
  acting on their own copy) keep the unaudited behaviour.
"""

from __future__ import annotations

from typing import Any, Optional

from code_indexer.server.services.audit_outcome import (
    conforming_details,
    record_outcome,
)

_ACTIVATED = "user_repo_activated_by_admin"
_DEACTIVATED = "user_repo_deactivated_by_admin"


def _record(
    actor: str,
    action_type: str,
    username: Optional[str],
    outcome: str,
    **detail_candidates: Any,
) -> None:
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="user",
        target_id=username,
        outcome=outcome,
        details=conforming_details(action_type, **detail_candidates),
    )


def activate_repository_for_user(
    manager: Any,
    username: str,
    golden_repo_alias: str,
    *,
    user_alias: str,
    actor: str,
) -> str:
    """Submit activation of *golden_repo_alias* for *username* on *actor*'s behalf."""
    if actor == username:
        return str(
            manager.activate_repository(
                username=username,
                golden_repo_alias=golden_repo_alias,
                user_alias=user_alias,
            )
        )
    try:
        job_id = str(
            manager.activate_repository(
                username=username,
                golden_repo_alias=golden_repo_alias,
                user_alias=user_alias,
            )
        )
    except Exception:
        _record(actor, _ACTIVATED, None, "failure")
        raise
    _record(
        actor,
        _ACTIVATED,
        username,
        "success",
        user_alias=user_alias,
        golden_repo_alias=golden_repo_alias,
        job_id=job_id,
    )
    return job_id


def deactivate_repository_for_user(
    manager: Any, username: str, user_alias: str, *, actor: str
) -> str:
    """Submit removal of *username*'s activated *user_alias* on *actor*'s behalf."""
    if actor == username:
        return str(
            manager.deactivate_repository(
                username=username, user_alias=user_alias, actor_username=actor
            )
        )
    try:
        job_id = str(
            manager.deactivate_repository(
                username=username, user_alias=user_alias, actor_username=actor
            )
        )
    except Exception:
        _record(actor, _DEACTIVATED, None, "failure")
        raise
    _record(
        actor, _DEACTIVATED, username, "success", user_alias=user_alias, job_id=job_id
    )
    return job_id
