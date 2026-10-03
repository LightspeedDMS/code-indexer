"""Shared leader gate for fleet-wide background work.

Why this module exists
----------------------
The lifespan runs once per uvicorn **worker process**, not once per pod. On the
``tools-*`` deployment that is 6 replicas x ``--workers 2`` = **12 processes**,
each with its own event loop, each firing the same startup sweeps against the
same shared Postgres and the same ``hard`` NFSv3 mount holding ~900 golden
repos. Work that is per-node is fine that way; work that is *fleet-wide* is not.

The decision this module encodes is deliberately narrow: **who may run it**, and
nothing else. It is the "one process decides" primitive (M1). It is not a work
queue -- when the work is divisible and should be shared, the right answer is
``POD_PULL_OPS`` + ``DistributedJobClaimer`` (M2), where every pod pulls what it
has memory for. Conflating the two is how a leader gate ends up serialising work
that should have fanned out.

The predicate itself was already here, as ``resolve_sweep_leader`` inside
``services/fts_backfill/scheduler.py``. It is moved here unchanged because a
general gate living inside one feature's scheduler is how the next caller ends
up writing a fifth copy of ``getattr(app_state, "leader_election", None)``.
That module re-exports it, so existing imports and their tests keep working.

Fail-open, on purpose
---------------------
Both entry points answer "yes, run" when the election cannot be consulted. Every
caller is a **self-heal** sweep: reconciling a registry, reclaiming leaked
snapshots, expiring stale leases. Before this gate existed they ran
unconditionally on all 12 processes, so fail-open is never worse than the status
quo -- while fail-closed would invent a new way for a fleet to quietly stop
healing itself, which is the harder failure to notice and the worse one to have.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# How long a one-shot startup sweep waits for leadership to settle before giving
# up and letting whoever won do the work.
#
# A periodic loop can afford to poll forever -- it gets another cycle. A one-shot
# cannot: it is evaluated DURING startup, and the process that is about to become
# leader may well reach the gate before it has won. Skip on that first read and
# the sweep runs nowhere, on any pod, until the next restart.
#
# The two delays it has to cover: the cluster-services block flipping
# ``scip_reconcile_leader_gate_ready`` (same lifespan, a few seconds later), and
# ``LeaderElectionService``'s 10 s monitor interval. 120 s is several times both.
# Overshooting costs a sleeping coroutine on the followers; undershooting costs a
# skipped sweep, so the asymmetry is priced in on purpose.
DEFAULT_LEADERSHIP_TIMEOUT_SECONDS = 120.0
DEFAULT_LEADERSHIP_POLL_SECONDS = 1.0


def resolve_sweep_leader(app_state: Any) -> bool:
    """Whether THIS process should run fleet-wide work, read from ``app.state``.

    Pure and duck-typed -- it reads two attributes and nothing else -- so the
    real production wiring can be unit-tested directly rather than through a
    fake injected at the call site.

    * Hold (``False``) until leader-election wiring is ready. Reuses the
      EVO-66188 ``scip_reconcile_leader_gate_ready`` startup gate: it is set to
      its final value early in lifespan and flips ``True`` in the cluster block's
      ``finally``, strictly *after* ``app.state.leader_election`` is populated,
      so a reader that sees ready can trust the election attribute.
    * Once ready, defer to the elected leader.
    * Single-node / sqlite has no election (``leader_election`` stays ``None``)
      -> always leader, so behaviour there is exactly as before.
    """
    try:
        if not getattr(app_state, "scip_reconcile_leader_gate_ready", True):
            return False
        leader_election = getattr(app_state, "leader_election", None)
        return leader_election is None or bool(leader_election.is_leader)
    except Exception:  # noqa: BLE001 -- see module docstring: fail OPEN
        logger.warning(
            "Leader gate could not be evaluated; running the work on this node "
            "(fail-open, pre-gate behaviour)",
            exc_info=True,
        )
        return True


async def await_sweep_leadership(
    app_state: Any,
    *,
    sweep_name: str,
    timeout: float = DEFAULT_LEADERSHIP_TIMEOUT_SECONDS,
    poll: float = DEFAULT_LEADERSHIP_POLL_SECONDS,
) -> bool:
    """Wait (bounded) for this process to become the fleet leader.

    Returns ``True`` if this process should do the work, ``False`` if it should
    skip because another process is the leader.

    Single-node returns ``True`` on the first check with no sleeping at all, so
    solo startup does not get measurably slower.

    ``timeout=0`` means "check once, do not wait" -- not "never run".

    Cancellation propagates: a follower parked on this must not hold up shutdown.
    """
    if resolve_sweep_leader(app_state):
        return True

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    step = max(0.01, poll)

    while loop.time() < deadline:
        await asyncio.sleep(min(step, max(0.0, deadline - loop.time())))
        if resolve_sweep_leader(app_state):
            return True

    logger.info(
        "%s: another node is the elected leader; skipping on this node",
        sweep_name,
    )
    return False
