"""The audited entry point of an admin-requested server restart.

The row must outlive the restart it announces, so it is written FIRST: the
action type is DURABLE, and :func:`request_server_restart` records it on the
calling thread (committed before it returns) and only then runs the trigger
that starts the restart.  A ``success`` row therefore means "requested",
never "completed".  When the trigger itself raises, a ``failure`` row
follows (carrying the same request's correlation id) and the exception
propagates.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from code_indexer.server.services.audit_outcome import record_outcome

_ACTION = "server_restart_requested"
_T = TypeVar("_T")


def request_server_restart(trigger: Callable[[], _T], *, actor: str, scope: str) -> _T:
    """Record *actor*'s restart request of *scope* ("node" / "cluster"), then run *trigger*."""
    record_outcome(
        actor=actor,
        action_type=_ACTION,
        target_type="server",
        target_id=scope,
        outcome="success",
    )
    try:
        return trigger()
    except Exception:
        record_outcome(
            actor=actor,
            action_type=_ACTION,
            target_type="server",
            target_id=scope,
            outcome="failure",
        )
        raise
