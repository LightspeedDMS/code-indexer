"""Internal shard router (C6 Phase 1, "B2 internal proxy").

kube-proxy load-balances /api/query to a random pod. When a single-repo query
lands on a pod that does not own the repo, that pod forwards the request to an
*owner* pod (which has the repo's index cached) over the pod network and returns
the owner's response. This eliminates the cold-load thrash that Phase 0 only
bounded the memory of.

Design:
- Only single-repo queries are routed (Phase 1). Omni/multi/wildcard stay
  single-pod for now (Phase 2 scatter-gather).
- The forwarded request carries the caller's Authorization header (the owner
  re-validates it, so tenancy/identity is preserved) and a loop-guard header so
  the owner serves locally instead of forwarding again.
- Everything **fails open**: no shard ownership (solo), we own the alias, no
  reachable owner, or any error -> serve locally (today's behaviour). A routing
  problem degrades to a cold local load, never a failed query.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Loop-guard header: set on a forwarded request so the receiving owner serves it
# locally rather than forwarding again.
FORWARD_HEADER = "X-CIDX-Shard-Forwarded"


class ShardRouter:
    """Decide whether a single-repo query should be forwarded, and forward it.

    Args:
        node_id: This node's cluster id.
        shard_ownership: ShardOwnership instance, or None in solo mode.
        node_addresses_provider: Callable returning ``{node_id: "host:port"}`` for
            reachable peers (from cluster_nodes).
        http_client_factory: HttpClientFactory used for the pod-to-pod POST (all
            outbound HTTP goes through it, per the fault-injection contract).
        timeout_seconds: Per-forward timeout.
    """

    def __init__(
        self,
        node_id: str,
        shard_ownership: Any,
        node_addresses_provider: Callable[[], Dict[str, str]],
        http_client_factory: Any,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._node_id = node_id
        self._shard_ownership = shard_ownership
        self._addresses = node_addresses_provider
        self._http = http_client_factory
        self._timeout = timeout_seconds
        # Coarse observability counters (metric-grade; small races acceptable).
        self._forwards = 0
        self._forward_failures = 0

    def target_for(self, alias: str) -> Optional[str]:
        """Return an owner peer ``"host:port"`` to forward ``alias`` to, or None
        to serve locally (we own it, solo mode, or no reachable owner)."""
        shard_ownership = self._shard_ownership
        if shard_ownership is None:
            return None  # solo / sharding off
        if shard_ownership.owns(alias):
            return None  # this node owns it -> serve locally
        owners = shard_ownership.owners_of(alias)
        if not owners:
            return None  # unknown ownership -> serve locally (fail open)
        try:
            addresses = self._addresses() or {}
        except Exception:
            logger.warning("C6: node address lookup failed; serving %s locally", alias)
            return None
        for owner in owners:
            if owner == self._node_id:
                continue
            addr = addresses.get(owner)
            if addr:
                return addr
        return None  # no reachable non-self owner -> serve locally

    def group_by_owner(
        self, aliases: "list[str]"
    ) -> "tuple[list[str], dict[str, list[str]]]":
        """Partition ``aliases`` for a multi-repo query (C6 Phase 2).

        Returns ``(local, {peer_address: [aliases]})`` where ``local`` are the
        aliases this node should search itself -- ones it owns, or for which no
        reachable owner exists (fail open) -- and each remaining group is the set
        of aliases to forward to a single owning peer.
        """
        local: "list[str]" = []
        groups: "dict[str, list[str]]" = {}
        for alias in aliases:
            address = self.target_for(alias)
            if address is None:
                local.append(alias)
            else:
                groups.setdefault(address, []).append(alias)
        return local, groups

    def forward(
        self,
        address: str,
        body: Dict[str, Any],
        auth_header: Optional[str],
        path: str = "/api/query",
    ) -> Dict[str, Any]:
        """POST ``body`` to a peer's ``path`` and return its JSON response.

        Raises on transport/HTTP error so the caller can fall back to local
        handling. Sets the loop-guard header so the peer serves locally instead
        of forwarding/scattering again.
        """
        headers: Dict[str, str] = {FORWARD_HEADER: "1"}
        if auth_header:
            headers["Authorization"] = auth_header
        self._forwards += 1
        try:
            with self._http.create_sync_client(timeout=self._timeout) as client:
                resp = client.post(
                    f"http://{address}{path}", json=body, headers=headers
                )
                resp.raise_for_status()
                result: Dict[str, Any] = resp.json()
                return result
        except Exception:
            self._forward_failures += 1
            raise

    def stats(self) -> Dict[str, Any]:
        """Observability snapshot: node identity, live ring, addresses, counters."""
        info: Dict[str, Any] = {
            "node_id": self._node_id,
            "forwards": self._forwards,
            "forward_failures": self._forward_failures,
        }
        if self._shard_ownership is not None:
            info.update(self._shard_ownership.snapshot())
        try:
            info["node_addresses"] = self._addresses()
        except Exception:
            info["node_addresses"] = {}
        return info
