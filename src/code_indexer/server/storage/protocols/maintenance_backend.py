"""MaintenanceBackend Protocol (Story #529).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class MaintenanceBackend(Protocol):
    """Protocol for maintenance mode state storage (Story #529).

    Provides cluster-wide coordination of maintenance mode by persisting
    state to the shared storage backend (PostgreSQL in cluster mode,
    SQLite in standalone mode).

    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a MaintenanceBackend without inheritance.
    """

    def enter_maintenance(self, started_by: str, reason: str, started_at: str) -> None:
        """Persist maintenance mode as active.

        Args:
            started_by: Username or identifier of who activated maintenance mode.
            reason: Human-readable reason for entering maintenance mode.
            started_at: ISO 8601 timestamp when maintenance mode was activated.
        """
        ...

    def exit_maintenance(self) -> None:
        """Mark maintenance mode as inactive (disable it)."""
        ...

    def get_status(self) -> "Optional[Dict[str, Any]]":
        """Return current maintenance state.

        Returns:
            Dict with keys: enabled (bool), reason (str or None),
            started_at (str or None), started_by (str or None).
            Always returns a dict (never None); enabled=False when inactive.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
