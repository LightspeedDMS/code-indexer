"""HNSWOrphanSweepStateBackend Protocol (Story #1360, Epic #1333 S3).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class HNSWOrphanSweepStateBackend(Protocol):
    """Protocol for HNSW orphan repair fleet sweep durable state
    (Story #1360, Epic #1333 S3)."""

    def get_state(self) -> Dict[str, Any]: ...

    def record_item_processed(self, key: str, outcome: str) -> None: ...

    def complete_pass(self) -> None: ...

    def cleanup_old_history(self, cutoff_iso: str) -> int:
        """Delete dependency_map_run_history records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None: ...
