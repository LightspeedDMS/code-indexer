"""
Leader Election Service (Story #423).

Implements cluster-wide leader election using PostgreSQL's pg_try_advisory_lock
so that exactly one node runs scheduler services at a time.

The advisory lock is held on a DEDICATED connection (not from the pool).
When the connection is closed (gracefully or due to a crash), PostgreSQL
automatically releases the lock, allowing another node to acquire it.

Usage::

    service = LeaderElectionService(
        connection_string="postgresql://user:pass@host/db",
        node_id="node-abc123",
    )
    service.register_leader_callbacks(
        on_become_leader=start_schedulers,
        on_lose_leadership=stop_schedulers,
    )
    service.start_monitoring()

    # Later, on shutdown:
    service.stop_monitoring()
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# "CIDX_LDR" encoded as a 64-bit integer for pg_advisory_lock.
# This value must be the same on all cluster nodes.
_LOCK_ID = 0x434944585F4C4452


class LeaderElectionService:
    """
    Leader election via pg_advisory_lock for scheduler singleton.

    Exactly one node in the cluster may hold the advisory lock at a time.
    The lock is acquired (and held) on a DEDICATED psycopg v3 connection
    that lives for as long as this node is leader.  Closing or losing that
    connection automatically releases the lock so another node can step up.
    """

    LOCK_ID = _LOCK_ID

    def __init__(self, connection_string: str, node_id: str) -> None:
        """
        Initialise the service.

        Args:
            connection_string: PostgreSQL DSN (psycopg v3 format).
            node_id:           Unique identifier for this cluster node
                               (used only for logging).
        """
        self._connection_string = connection_string
        self._node_id = node_id
        self._is_leader: bool = False
        self._lock_conn = None  # Dedicated connection holding the advisory lock
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_become_leader: Optional[Callable[[], None]] = None
        self._on_lose_leadership: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        """Whether this node currently holds leadership."""
        return self._is_leader

    def register_leader_callbacks(
        self,
        on_become_leader: Callable[[], None],
        on_lose_leadership: Callable[[], None],
    ) -> None:
        """
        Register callbacks for leadership transitions.

        Args:
            on_become_leader:  Called when this node acquires the lock.
            on_lose_leadership: Called when this node loses the lock
                                (e.g., connection error detected by monitor).
        """
        self._on_become_leader = on_become_leader
        self._on_lose_leadership = on_lose_leadership

    def try_acquire_leadership(self) -> bool:
        """
        Attempt to acquire the advisory lock on a dedicated connection.

        Opens a new dedicated connection and calls
        ``SELECT pg_try_advisory_lock(LOCK_ID)``.  If the lock is acquired,
        the connection is kept open (stored in ``self._lock_conn``) so that
        the lock remains held.

        Returns:
            True  — lock acquired; this node is now leader.
            False — lock already held by another node; connection closed.

        Does NOT raise on database errors — logs them and returns False.
        """
        try:
            import psycopg  # lazy import — keeps startup fast
        except ImportError:  # pragma: no cover
            logger.error(
                "LeaderElectionService: psycopg (v3) is not installed; "
                "leader election is not available."
            )
            return False

        try:
            conn = psycopg.connect(
                self._connection_string,
                autocommit=True,
                # TCP keepalive: detect dead connections within ~25 seconds
                # (idle=10s, interval=5s, probes=3) to prevent ghost leader
                # on network partition.
                keepalives=1,
                keepalives_idle=10,
                keepalives_interval=5,
                keepalives_count=3,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
                row = cur.fetchone()
                acquired = bool(row[0]) if row else False

            if acquired:
                self._lock_conn = conn
                was_leader = self._is_leader
                self._is_leader = True
                logger.info(
                    "LeaderElectionService [%s]: acquired leadership (lock_id=%s)",
                    self._node_id,
                    hex(_LOCK_ID),
                )
                if not was_leader and self._on_become_leader is not None:
                    try:
                        self._on_become_leader()
                    except Exception:
                        logger.exception(
                            "LeaderElectionService [%s]: on_become_leader callback raised",
                            self._node_id,
                        )
                return True
            else:
                conn.close()
                logger.debug(
                    "LeaderElectionService [%s]: lock already held by another node",
                    self._node_id,
                )
                return False

        except Exception:
            logger.exception(
                "LeaderElectionService [%s]: error trying to acquire leadership",
                self._node_id,
            )
            return False

    def release_leadership(self) -> None:
        """
        Release the advisory lock by closing the dedicated connection.

        PostgreSQL automatically releases the advisory lock when the
        connection closes.  If this node was leader, the on_lose_leadership
        callback is invoked.
        """
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()
            except Exception:
                logger.exception(
                    "LeaderElectionService [%s]: error closing lock connection",
                    self._node_id,
                )
            finally:
                self._lock_conn = None

        was_leader = self._is_leader
        self._is_leader = False

        if was_leader:
            logger.info(
                "LeaderElectionService [%s]: released leadership", self._node_id
            )
            if self._on_lose_leadership is not None:
                try:
                    self._on_lose_leadership()
                except Exception:
                    logger.exception(
                        "LeaderElectionService [%s]: on_lose_leadership callback raised",
                        self._node_id,
                    )

    def start_monitoring(self, check_interval: int = 10) -> None:
        """
        Start a background thread that periodically tries to acquire leadership.

        If this node is not yet leader, it will keep retrying every
        ``check_interval`` seconds.  If the current leader dies (its
        connection drops), the lock is released by PostgreSQL and this
        node can acquire it on the next iteration.

        Also monitors whether the existing dedicated connection is still
        alive; if it has been lost, leadership is relinquished and the
        node will attempt re-election on the next iteration.

        Args:
            check_interval: Seconds between election attempts (default 10).
        """
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            logger.warning(
                "LeaderElectionService [%s]: monitor already running", self._node_id
            )
            return

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(check_interval,),
            daemon=True,
            name=f"LeaderElection-{self._node_id}",
        )
        self._monitor_thread.start()
        logger.info(
            "LeaderElectionService [%s]: monitor started (interval=%ds)",
            self._node_id,
            check_interval,
        )

    def stop_monitoring(self) -> None:
        """
        Stop the monitoring thread and release leadership.

        Waits up to ``check_interval + 2`` seconds for the thread to exit.
        """
        self._stop_event.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=15)
        self._monitor_thread = None
        self.release_leadership()
        logger.info("LeaderElectionService [%s]: monitor stopped", self._node_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self, check_interval: int) -> None:
        """
        Background loop: check/attempt leadership every ``check_interval`` s.

        On each iteration:
        1. If we are leader, verify the dedicated connection is still alive.
           If not, relinquish leadership and attempt re-election.
        2. If we are not leader, try to acquire it.
        """
        while not self._stop_event.is_set():
            try:
                if self._is_leader:
                    if not self._connection_alive():
                        logger.warning(
                            "LeaderElectionService [%s]: lock connection lost; "
                            "relinquishing leadership",
                            self._node_id,
                        )
                        # Null out the dead connection before releasing
                        self._lock_conn = None
                        was_leader = self._is_leader
                        self._is_leader = False
                        if was_leader and self._on_lose_leadership is not None:
                            try:
                                self._on_lose_leadership()
                            except Exception:
                                logger.exception(
                                    "LeaderElectionService [%s]: on_lose_leadership raised",
                                    self._node_id,
                                )
                        # Defer re-election to the next monitor loop iteration
                        # so on_lose_leadership() has completed before we
                        # attempt to re-acquire the lock.
                else:
                    self.try_acquire_leadership()
            except Exception:
                logger.exception(
                    "LeaderElectionService [%s]: unexpected error in monitor loop",
                    self._node_id,
                )

            self._stop_event.wait(check_interval)

    def _connection_alive(self) -> bool:
        """
        Return True if the dedicated lock connection is still alive.

        Uses a lightweight ``SELECT 1`` ping.
        """
        if self._lock_conn is None:
            return False
        try:
            with self._lock_conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False
