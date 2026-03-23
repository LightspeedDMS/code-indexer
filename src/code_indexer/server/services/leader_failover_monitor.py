"""
Leader Failover Monitor (Story #424).

Monitors leadership status and handles failover transitions by wrapping a
LeaderElectionService instance.  The monitor runs a background thread that
periodically attempts to acquire leadership (when not leader) and verifies the
lock connection is still alive (when leader).

Leadership-transition callbacks are registered here and forwarded to the
underlying LeaderElectionService so that callers deal only with this class.

Usage::

    election_service = LeaderElectionService(
        connection_string="postgresql://user:pass@host/db",
        node_id="node-abc123",
    )

    monitor = LeaderFailoverMonitor(election_service, check_interval=10)
    monitor.register_callbacks(
        on_become_leader=start_schedulers,
        on_lose_leadership=stop_schedulers,
    )
    monitor.start()

    # On shutdown:
    monitor.stop()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from code_indexer.server.services.leader_election_service import (
        LeaderElectionService,
    )

logger = logging.getLogger(__name__)


class LeaderFailoverMonitor:
    """Monitors leadership status and handles failover transitions.

    This class is a thin wrapper around :class:`LeaderElectionService` that
    provides a simplified lifecycle interface (``start`` / ``stop``) and
    callback registration.  The actual advisory-lock logic and the background
    monitoring thread live inside ``LeaderElectionService``; this class
    forwards callbacks and delegates monitoring to it.

    Args:
        leader_election: An already-constructed ``LeaderElectionService``.
        check_interval:  Seconds between election/health-check iterations
                         (default 10).
    """

    def __init__(
        self,
        leader_election: "LeaderElectionService",
        check_interval: int = 10,
    ) -> None:
        self._leader_election = leader_election
        self._check_interval = check_interval
        self._on_become_leader: Optional[Callable[[], None]] = None
        self._on_lose_leadership: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        """Delegate to the underlying election service."""
        return bool(self._leader_election.is_leader)

    def register_callbacks(
        self,
        on_become_leader: Callable[[], None],
        on_lose_leadership: Callable[[], None],
    ) -> None:
        """Register callbacks for leadership transitions.

        The callbacks are forwarded to the underlying
        :class:`LeaderElectionService` so that they fire on the appropriate
        advisory-lock events.

        Args:
            on_become_leader:   Called when this node acquires the lock.
            on_lose_leadership: Called when this node loses the lock.
        """
        self._on_become_leader = on_become_leader
        self._on_lose_leadership = on_lose_leadership
        self._leader_election.register_leader_callbacks(
            on_become_leader=on_become_leader,
            on_lose_leadership=on_lose_leadership,
        )
        logger.debug("LeaderFailoverMonitor: callbacks registered")

    def start(self) -> None:
        """Start the background monitoring thread.

        Delegates to :meth:`LeaderElectionService.start_monitoring` using the
        configured ``check_interval``.  The background thread is a daemon
        thread (set by ``LeaderElectionService``) so it does not block process
        shutdown.
        """
        logger.info(
            "LeaderFailoverMonitor: starting (check_interval=%ds)",
            self._check_interval,
        )
        self._leader_election.start_monitoring(check_interval=self._check_interval)

    def stop(self) -> None:
        """Stop the background monitoring thread gracefully.

        Delegates to :meth:`LeaderElectionService.stop_monitoring`, which
        signals the thread to exit, joins it, and releases the advisory lock
        (firing ``on_lose_leadership`` if this node was leader).
        """
        logger.info("LeaderFailoverMonitor: stopping")
        self._leader_election.stop_monitoring()
