"""Rebalance pre-warming for repo sharding (C6).

When cluster membership changes, HRW ownership reshuffles a minimal set of repos
to new owners. Those newly-owned repos are cold in the new owner's cache, so the
first query pays a cold load. This service proactively warms (loads into the
per-pod index cache) the repos this node owns, so the first post-rebalance query
is already hot.

Design:
- A daemon loop re-evaluates ownership every ``interval_seconds``. Because
  ShardOwnership.owns() reflects the live node set, a membership change simply
  changes which repos this node owns -> newly-owned repos get warmed, and repos
  it no longer owns are dropped from the warmed set (so they are re-warmed if
  ownership swings back; the cache evicts them via TTL).
- The warm operation is injected (``warm_fn``) and every call is best-effort:
  a failed warm is logged and retried next cycle, never raised. So a warm
  problem only means a repo stays cold until its first query -- today's
  behaviour -- and never destabilises the pod.
- No-op unless there is a shard ownership (cluster mode).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional, Set

from code_indexer.server.services.shard_ownership import ShardOwnership

logger = logging.getLogger(__name__)


class ShardPrewarmService:
    """Warms the index cache for repos this node owns (C6 rebalance prewarm).

    Args:
        shard_ownership: ShardOwnership; None disables the service (solo mode).
        repos_provider: Returns the current set of shardable repo aliases.
        warm_fn: Warms one repo (loads its index into the per-pod cache). Called
            best-effort; exceptions are caught.
        interval_seconds: How often to re-evaluate ownership and warm.
    """

    def __init__(
        self,
        shard_ownership: Optional[ShardOwnership],
        repos_provider: Callable[[], List[str]],
        warm_fn: Callable[[str], None],
        interval_seconds: float = 30.0,
    ) -> None:
        self._ownership = shard_ownership
        self._repos_provider = repos_provider
        self._warm_fn = warm_fn
        self._interval = interval_seconds
        self._warmed: Set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> None:
        """One evaluation: warm newly-owned repos, forget no-longer-owned ones.

        Never raises -- individual warm failures are logged and retried next
        cycle.
        """
        ownership = self._ownership
        if ownership is None:
            return
        try:
            all_repos = list(self._repos_provider() or [])
        except Exception:
            logger.warning("C6 prewarm: could not list repos", exc_info=True)
            return
        owned = {alias for alias in all_repos if ownership.owns(alias)}
        # Forget repos this node no longer owns (or that were deleted) so they are
        # re-warmed if ownership swings back; the cache evicts them via TTL.
        self._warmed &= owned
        for alias in owned - self._warmed:
            try:
                self._warm_fn(alias)
                self._warmed.add(alias)
            except Exception:
                logger.warning(
                    "C6 prewarm: failed to warm %r; will retry", alias, exc_info=True
                )

    def start(self) -> None:
        """Start the background warm loop (no-op in solo mode or if running)."""
        if self._ownership is None:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cidx-shard-prewarm", daemon=True
        )
        self._thread.start()
        logger.info("C6 prewarm: started (interval=%ss)", self._interval)

    def stop(self) -> None:
        """Signal the loop to stop and join briefly."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 5)
        self._thread = None

    def _loop(self) -> None:
        # Wait one interval before the first pass. Server startup wires the
        # http_client_factory that the warm search uses, and it runs *after* this
        # service is started, so an immediate first pass would fail. Warming is
        # proactive, so a short initial delay is fine. wait() returns True when
        # stopped, so the loop also exits promptly on shutdown.
        while not self._stop.wait(self._interval):
            self.run_once()
