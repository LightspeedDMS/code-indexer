"""Single source of truth for resolving this process's cluster node identity.

Discovered via a real e2e-automation.sh Phase 6 (PostgreSQL Parity) run: two
independent call sites derived "this node's identity" from the same
config.json shape (``cluster.node_id``) but disagreed on the FALLBACK when
that key is absent -- a legitimate, supported single-node
``storage_mode=postgres`` deployment (exactly what the Phase 6 E2E harness's
``write_pg_bootstrap_config`` produces).

- ``startup/service_init.py`` (before this fix) defaulted to the literal
  string ``"local"``. This value is threaded into ``JobTracker(node_id=...)``
  and stamped onto every job row's ``executing_node`` column at creation time
  (``register_job_if_no_conflict`` -> ``_atomic_insert_impl``, default
  ``stamp_executing_node=True``).
- ``startup/lifespan.py``'s cluster-services block (unchanged) defaults to
  ``f"{hostname}-cidx"``. This value is what ``NodeHeartbeatService``
  registers into the ``cluster_nodes`` table, i.e. what
  ``get_active_nodes()`` returns, and what ``DistributedJobClaimer`` uses to
  claim/execute jobs.

Because the two defaults disagreed, any job whose row was stamped with the
``JobTracker``-side identity (e.g. ``GoldenRepoManager.
_register_lifecycle_after_registration``'s ``lifecycle_registration`` job,
which transitions to ``status='running'`` in-process and never sets
``claimed_at``) could never match its own node in
``get_active_nodes()``. ``JobReconciliationService._reclaim_dead_node_jobs``
therefore treated the job as abandoned by a dead node on the very next sweep
(default every 5s) -- even though the node was alive and the job genuinely,
correctly still running -- and reset it to ``pending``.
``DistributedJobWorkerService`` then claimed the now-pending row and
immediately failed it, since ``lifecycle_registration`` is (correctly, for
genuinely-abandoned jobs) not in its ``RETRYABLE_JOB_TYPES`` allow-list.

Both call sites (``startup/service_init.py`` and ``startup/lifespan.py``'s
cluster-services block) now resolve through this ONE function so they can
never diverge again. The ``f"{hostname}-cidx"`` convention was chosen as the
shared DEFAULT (rather than harmonizing on ``"local"``) because it is the
identity actually persisted in ``cluster_nodes``/``get_active_nodes()`` --
the value every liveness check in the cluster path is compared against.
"""

from __future__ import annotations

import socket
from typing import Any, Dict, Optional


def resolve_cluster_node_id(raw_config: Optional[Dict[str, Any]]) -> str:
    """Resolve this process's cluster node identity from a parsed
    ``config.json`` dict.

    Precedence:
      1. Explicit, non-empty STRING ``cluster.node_id`` from *raw_config* --
         honored verbatim, even if an operator explicitly configured it to
         the literal string ``"local"`` (that is a deliberate, explicit
         choice and is not second-guessed here). A non-string value (e.g.
         an int written by a malformed config) is treated as absent rather
         than coerced, falling through to path 2.
      2. ``f"{hostname}-cidx"`` -- the same synthetic identity
         ``NodeHeartbeatService``/``LeaderElectionService`` register under
         when no explicit node_id is configured.

    Args:
        raw_config: The parsed ``config.json`` dict, or ``None``/``{}``/any
            other non-dict value when unavailable or malformed. Never
            mutated.

    Returns:
        A non-empty node identity string. The DEFAULT fallback (path 2
        above, used whenever ``cluster.node_id`` is absent/empty/not a
        string, or *raw_config*/``cluster`` is malformed) is never the
        literal ``"local"`` -- that value can never appear in
        ``get_active_nodes()``, and using it as the fallback here is
        precisely the defect this function exists to eliminate.
    """
    cluster_cfg = raw_config.get("cluster") if isinstance(raw_config, dict) else None
    configured_node_id = (
        cluster_cfg.get("node_id") if isinstance(cluster_cfg, dict) else None
    )
    if isinstance(configured_node_id, str) and configured_node_id:
        return configured_node_id
    return f"{socket.gethostname()}-cidx"
