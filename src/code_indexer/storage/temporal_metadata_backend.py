"""Storage-engine-agnostic Protocol for temporal metadata storage.

Bug #1313: TemporalMetadataStore (Story #669) was a SQLite-WAL database that,
in cluster mode, lives on the shared NFS golden-repos mount. NFS cannot
satisfy SQLite WAL's `-shm` requirement, and every commit paid an NFS fsync,
serializing all indexing threads. The fix keeps the store's schema/operations
IDENTICAL and makes only the storage ENGINE pluggable: SQLite for CLI/solo
(unchanged), PostgreSQL for cluster mode (via the process-level backend
registry in temporal_metadata_backend_registry.py).

This Protocol lives in the CORE layer (not server/storage/protocols.py)
because TemporalMetadataStore itself is core -- imported by the CLI/solo
indexing path, which must never import anything from code_indexer.server.*.

``generate_hash_prefix`` is intentionally NOT part of this Protocol: it is a
pure, stateless, deterministic function of point_id (sha256(point_id)[:16])
shared as a single module-level implementation in temporal_metadata_store.py,
not a per-backend behavior.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Set, Tuple, runtime_checkable


@runtime_checkable
class TemporalMetadataBackend(Protocol):
    """Protocol for temporal metadata storage engines (SQLite or PostgreSQL).

    Implementations MUST preserve the exact write-then-read semantics of the
    original SQLite-only TemporalMetadataStore: `save_metadata`/
    `save_metadata_batch` are UPSERTs keyed by hash_prefix (derived from
    point_id via the shared `generate_hash_prefix`), and all reads are scoped
    to whatever collection identity the backend was constructed with.
    """

    def save_metadata_batch(self, rows: List[Tuple[str, Dict]]) -> List[str]:
        """Save metadata for multiple points in ONE transaction/commit.

        Args:
            rows: List of (point_id, payload) tuples.

        Returns:
            List of 16-char hash prefixes in the same order as input rows.
        """
        ...

    def save_metadata(self, point_id: str, payload: Dict) -> str:
        """Save metadata for a single point and return its hash prefix."""
        ...

    def checkpoint_wal(self) -> None:
        """Bound WAL/log growth. No-op for backends without a WAL concept."""
        ...

    def get_point_id(self, hash_prefix: str) -> Optional[str]:
        """Retrieve point_id from hash prefix, or None if not found."""
        ...

    def get_metadata(self, hash_prefix: str) -> Optional[Dict]:
        """Retrieve full metadata dict from hash prefix, or None if not found."""
        ...

    def delete_metadata(self, hash_prefix: str) -> None:
        """Delete a metadata entry (no-op if it does not exist)."""
        ...

    def cleanup_stale_metadata(self, valid_hash_prefixes: Set[str]) -> int:
        """Remove entries without a corresponding vector file.

        Args:
            valid_hash_prefixes: Set of hash prefixes that have vector files.

        Returns:
            Number of stale entries removed.
        """
        ...

    def count_entries(self) -> int:
        """Count total metadata entries."""
        ...
