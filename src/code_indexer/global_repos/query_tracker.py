"""
Query Tracker for reference counting active queries.

Provides thread-safe reference counting for tracking active queries
against different index versions. Used to determine when old indexes
can be safely cleaned up after alias swaps.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Dict, Set


logger = logging.getLogger(__name__)


class QueryTracker:
    """
    Thread-safe reference counter for active queries.

    Tracks how many active queries are using each index path.
    This enables query-aware cleanup: old indexes are only deleted
    when their reference count reaches zero.
    """

    def __init__(self):
        """Initialize the query tracker with empty ref counts."""
        self._ref_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
        # Story #1458 round-6 Codex HIGH finding #6b: an admission-barrier
        # primitive, distinct from ref counting. Deactivation marks a
        # path quiescing FIRST (atomically, under the same lock query
        # admission checks), so a NEW query arriving after that point is
        # refused rather than racing the drain-then-rename sequence.
        self._quiescing: Set[str] = set()

    def mark_quiescing(self, path: str) -> None:
        """Mark ``path`` as quiescing (about to be destructively purged).
        Query admission must refuse new refcount increments for this
        path once marked. Thread-safe."""
        with self._lock:
            self._quiescing.add(path)

    def clear_quiescing(self, path: str) -> None:
        """Clear the quiescing mark for ``path``. A no-op if ``path`` was
        never marked (e.g. a failed deactivation attempt cleaning up).
        Thread-safe."""
        with self._lock:
            self._quiescing.discard(path)

    def is_quiescing(self, path: str) -> bool:
        """True iff ``path`` is currently marked quiescing. Thread-safe."""
        with self._lock:
            return path in self._quiescing

    def try_increment_ref_if_not_quiescing(self, path: str) -> bool:
        """Atomically check quiescing status and increment the refcount,
        both inside ONE critical section (single lock acquisition).

        Round-8 HIGH finding (Codex empirical reproduction): a caller
        composing `is_quiescing()` then `increment_ref()` as two SEPARATE
        lock-acquiring calls leaves a TOCTOU window -- another thread can
        call `mark_quiescing()` for the SAME path in the gap between the
        check and the increment, and the increment still succeeds. This
        method closes that window by performing both steps under the
        SAME lock acquisition, so no other thread can observe or mutate
        quiescing/refcount state in between.

        Args:
            path: The index path to admit a query against.

        Returns:
            True if admitted (refcount incremented by 1). False if
            refused (``path`` is currently marked quiescing; refcount is
            left UNCHANGED).

        Thread-safe: single lock acquisition covers both the check and
        the mutation.
        """
        with self._lock:
            if path in self._quiescing:
                return False
            current = self._ref_counts.get(path, 0)
            self._ref_counts[path] = current + 1
            logger.debug(
                f"Incremented ref count for {path}: {current} -> {current + 1} "
                f"(atomic quiescing-gated admission)"
            )
            return True

    def increment_ref(self, index_path: str) -> None:
        """
        Increment reference count for an index path.

        Called when a query starts using an index.

        Args:
            index_path: Path to the index being queried

        Thread-safe: Uses lock for atomic increment
        """
        with self._lock:
            current = self._ref_counts.get(index_path, 0)
            self._ref_counts[index_path] = current + 1
            logger.debug(
                f"Incremented ref count for {index_path}: {current} -> {current + 1}"
            )

    def decrement_ref(self, index_path: str) -> None:
        """
        Decrement reference count for an index path.

        Called when a query completes.

        Args:
            index_path: Path to the index that was queried

        Raises:
            ValueError: If ref count would become negative (bug indicator)

        Thread-safe: Uses lock for atomic decrement
        """
        with self._lock:
            current = self._ref_counts.get(index_path, 0)

            if current <= 0:
                raise ValueError(
                    f"Reference count cannot be negative for {index_path}. "
                    "This indicates a bug (decrement without increment)."
                )

            new_count = current - 1
            self._ref_counts[index_path] = new_count

            # Clean up zero entries to keep dict small
            if new_count == 0:
                del self._ref_counts[index_path]

            logger.debug(
                f"Decremented ref count for {index_path}: {current} -> {new_count}"
            )

    def get_ref_count(self, index_path: str) -> int:
        """
        Get current reference count for an index path.

        Args:
            index_path: Path to check

        Returns:
            Current reference count (0 if path not tracked)

        Thread-safe: Lock-protected read
        """
        with self._lock:
            return self._ref_counts.get(index_path, 0)

    def get_all_paths(self) -> Set[str]:
        """
        Get all index paths with non-zero reference counts.

        Returns:
            Set of index paths currently in use

        Thread-safe: Lock-protected read with snapshot copy
        """
        with self._lock:
            # Return copy to prevent external mutation
            return set(self._ref_counts.keys())

    @contextmanager
    def track_query(self, index_path: str):
        """
        Context manager for automatic query tracking.

        Usage:
            with tracker.track_query(path):
                # Query executes here
                # Ref count is incremented
                ...
            # Ref count is decremented here (even on exception)

        Args:
            index_path: Path to the index being queried

        Yields:
            None

        Ensures:
            Ref count is decremented even if exception occurs
        """
        self.increment_ref(index_path)
        try:
            yield
        finally:
            self.decrement_ref(index_path)
