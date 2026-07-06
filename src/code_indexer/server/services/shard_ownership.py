"""Repo-shard ownership via rendezvous (HRW) hashing over live cluster nodes.

Phase 0 of repo sharding (C6). In cluster mode every pod otherwise loads every
repo's index into its own cache, so per-pod memory grows with the *total* repo
count and adding replicas does not help. This module lets each pod decide which
repos it "owns": only owned repos populate the per-pod index cache, while
non-owned repos are still served (load-and-discard), bounding each pod's cache
footprint to its shard.

Ownership is **stateless and deterministic**: every node computes the same
top-``replicas`` owners for an alias from the same live-node set (rendezvous /
highest-random-weight hashing), so no assignment table or cross-node
coordination is required. Membership changes reshuffle only the minimal set of
aliases (the property HRW is chosen for).

Design notes:
- The shard key is the **repo alias** (stable across refreshes), never the
  on-disk index path (which rotates every golden-repo refresh).
- ``owns()`` fails **open**: on any error, an empty node set, or a single-node
  cluster it returns True, degrading to today's cache-everything behaviour --
  never worse, never a dropped result.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Callable, List


def _hrw_score(alias: str, node_id: str) -> int:
    """Deterministic rendezvous weight for (alias, node_id). Higher wins."""
    digest = hashlib.blake2b(
        f"{alias}\x00{node_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big")


def compute_owners(alias: str, node_ids: List[str], replicas: int) -> List[str]:
    """Return the top-``replicas`` node_ids for ``alias`` by HRW score.

    Deterministic given the same inputs. ``node_id`` is used as a tie-breaker so
    the ordering is stable across nodes. ``replicas`` is clamped to
    ``[1, len(node_ids)]``.
    """
    if not node_ids:
        return []
    want = max(1, min(replicas, len(node_ids)))
    ranked = sorted(node_ids, key=lambda n: (_hrw_score(alias, n), n), reverse=True)
    return ranked[:want]


class ShardOwnership:
    """Decides whether THIS node owns (should cache) a given repo alias.

    Args:
        node_id: This node's cluster id (``cluster.node_id``).
        active_nodes_provider: Callable returning the current live node_ids
            (e.g. ``NodeHeartbeatService.get_active_nodes``). Called at most once
            per ``refresh_seconds`` and cached, so it is cheap to call per query.
        replicas: Replication factor -- how many nodes own each alias.
        refresh_seconds: How long to cache the active-node list.
        time_fn: Monotonic clock (injectable for tests).
    """

    def __init__(
        self,
        node_id: str,
        active_nodes_provider: Callable[[], List[str]],
        replicas: int = 2,
        refresh_seconds: float = 5.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._node_id = node_id
        self._provider = active_nodes_provider
        self._replicas = max(1, replicas)
        self._refresh_seconds = refresh_seconds
        self._time = time_fn
        self._lock = threading.Lock()
        self._cached_nodes: List[str] = []
        self._cached_at: float = 0.0

    def _get_nodes(self) -> List[str]:
        now = self._time()
        with self._lock:
            fresh = self._cached_nodes and (
                now - self._cached_at < self._refresh_seconds
            )
            if fresh:
                return self._cached_nodes
        # Refresh outside the lock; tolerate provider failures (fail open).
        try:
            nodes = list(self._provider() or [])
        except Exception:
            nodes = []
        # Ensure this node is considered part of the ring even if the heartbeat
        # row is briefly missing -- otherwise a node could disown everything.
        if self._node_id and self._node_id not in nodes:
            nodes.append(self._node_id)
        with self._lock:
            self._cached_nodes = nodes
            self._cached_at = now
        return nodes

    def owns(self, alias: str) -> bool:
        """True if this node should cache ``alias``.

        Fails open (returns True) for empty aliases, single-node clusters, or any
        error, so a query is never denied caching incorrectly.
        """
        if not alias:
            return True
        nodes = self._get_nodes()
        if len(nodes) <= 1:
            return True
        return self._node_id in compute_owners(alias, nodes, self._replicas)

    def owners_of(self, alias: str) -> List[str]:
        """Return the owner node_ids for ``alias`` over the current live set.

        Empty for an empty alias or empty node set. Used by the router to pick a
        peer to forward a non-owned query to.
        """
        if not alias:
            return []
        return compute_owners(alias, self._get_nodes(), self._replicas)

    def snapshot(self) -> "dict[str, object]":
        """Observability view: this node's id, replicas, and live node set."""
        return {
            "node_id": self._node_id,
            "replicas": self._replicas,
            "active_nodes": self._get_nodes(),
        }
