"""Temporal metadata storage for v2 format filename resolution.

Story #669: Fix Temporal Indexing Filename Length Issue
Bug #1313: Pluggable storage engine (SQLite for CLI/solo, PostgreSQL for
cluster mode) -- schema/operations are IDENTICAL, only the storage ENGINE is
selected at construction time via the process-level backend registry
(temporal_metadata_backend_registry.py).

This module provides metadata storage for temporal collections using v2
format (hash-based filenames). It maintains point_id-to-hash mappings with
full metadata to enable efficient queries and format detection.

V2 Format:
- Filenames: vector_{sha256(point_id)[:16]}.json (28 chars total)
- Metadata: point_id, commit_hash, file_path, chunk_index (SQLite or PostgreSQL)
- Detection: Presence of temporal_metadata.db file indicates v2 format (CLI);
  in PG mode, presence of at least one vector_<hex16>.json file (see
  detect_format below).

V1 Format (Legacy):
- Filenames: vector_{point_id_with_slashes_replaced}.json (can exceed 255 chars)
- No metadata database
- Detection: Absence of temporal_metadata.db indicates v1 format

TemporalMetadataStore is a FACADE: its public class name and constructor
signature (collection_path) are unchanged from the original SQLite-only
implementation, so every existing caller (filesystem_vector_store.py,
dashboard_service.py) requires zero changes. Internally, __init__ selects the
backend:
  - No factory registered (CLI, daemon, solo server) -> TemporalMetadataSqliteBackend
    (byte-for-byte identical behavior to the pre-#1313 implementation).
  - Factory registered (cluster/postgres lifespan startup) -> whatever backend
    the factory returns (TemporalMetadataPostgresBackend in production).
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .temporal_metadata_backend_registry import get_temporal_metadata_backend_factory

logger = logging.getLogger(__name__)

# 16-char SHA256 prefix for v2 format filenames. Shared module-level constant
# so generate_hash_prefix() (below) and any backend implementation compute
# hash prefixes identically without duplicating the magic number.
HASH_PREFIX_LENGTH = 16

# Length of the collection_key derived from the collection path (Bug #1313
# Step 5). Used by the PostgreSQL backend to scope rows to a single logical
# collection since, unlike SQLite (one file per collection), one PG table
# holds every collection's rows.
COLLECTION_KEY_LENGTH = 32


def generate_hash_prefix(point_id: str) -> str:
    """Generate 16-char SHA256 hash prefix for point_id.

    Bug #1313 Step 1: single shared implementation used by every backend
    (SQLite, PostgreSQL) so filenames/lookups derived from point_id are
    identical regardless of which backend is active.

    Args:
        point_id: Full point_id (e.g., "project:diff:hash:path:index")

    Returns:
        16-character SHA256 hash prefix
    """
    return hashlib.sha256(point_id.encode()).hexdigest()[:HASH_PREFIX_LENGTH]


class TemporalFormatError(Exception):
    """Raised when legacy v1 format is detected and requires re-indexing."""

    pass


class TemporalMetadataStore:
    """Backend-selecting facade for temporal collection metadata storage.

    Stores point_id-to-hash mappings with metadata:
    - hash_prefix: 16-char SHA256 prefix (used as filename)
    - point_id: Full point_id (original)
    - commit_hash: Git commit hash
    - file_path: File path from payload
    - chunk_index: Chunk index
    - created_at: Timestamp

    Schema (identical across backends):
        CREATE TABLE temporal_metadata (
            hash_prefix TEXT PRIMARY KEY,
            point_id TEXT NOT NULL UNIQUE,
            commit_hash TEXT,
            file_path TEXT,
            chunk_index INTEGER,
            created_at TEXT,
            format_version INTEGER DEFAULT 2
        );

    Bug #1313: every method below delegates to ``self._backend`` (a
    TemporalMetadataBackend). The backend is selected once, in __init__.
    """

    METADATA_DB_NAME = "temporal_metadata.db"
    HASH_PREFIX_LENGTH = HASH_PREFIX_LENGTH  # backward-compat class attribute

    def __init__(self, collection_path: Path):
        """Initialize temporal metadata store, selecting the storage backend.

        Args:
            collection_path: Path to the temporal collection directory
        """
        self.collection_path = collection_path
        self.db_path = collection_path / self.METADATA_DB_NAME
        self.collection_key = hashlib.sha256(str(collection_path).encode()).hexdigest()[
            :COLLECTION_KEY_LENGTH
        ]

        factory = get_temporal_metadata_backend_factory()
        if factory is not None:
            self._backend = factory(collection_path)
        else:
            # Local import: temporal_metadata_sqlite_backend.py imports
            # generate_hash_prefix back from this module, so importing it at
            # module level here would create a circular import at load time.
            from .temporal_metadata_sqlite_backend import TemporalMetadataSqliteBackend

            self._backend = TemporalMetadataSqliteBackend(collection_path)

    def generate_hash_prefix(self, point_id: str) -> str:
        """Generate 16-char SHA256 hash prefix for point_id.

        Bug #1313 Step 1: forwards to the shared module-level
        ``generate_hash_prefix`` so every backend derives identical prefixes.

        Args:
            point_id: Full point_id (e.g., "project:diff:hash:path:index")

        Returns:
            16-character SHA256 hash prefix
        """
        return generate_hash_prefix(point_id)

    def save_metadata(self, point_id: str, payload: Dict) -> str:
        """Save metadata for a point and return hash prefix.

        Args:
            point_id: Full point_id
            payload: Payload dict containing commit_hash, path, chunk_index

        Returns:
            16-char hash prefix (to be used as filename)
        """
        return self._backend.save_metadata(point_id, payload)

    def save_metadata_batch(self, rows: List[Tuple[str, Dict]]) -> List[str]:
        """Save metadata for multiple points in ONE transaction.

        Args:
            rows: List of (point_id, payload) tuples.

        Returns:
            List of 16-char hash prefixes in the same order as input rows.
        """
        return self._backend.save_metadata_batch(rows)

    def checkpoint_wal(self) -> None:
        """Bound WAL/log growth (no-op for backends without a WAL concept)."""
        self._backend.checkpoint_wal()

    def get_point_id(self, hash_prefix: str) -> Optional[str]:
        """Retrieve point_id from hash prefix.

        Args:
            hash_prefix: 16-char hash prefix

        Returns:
            Full point_id if found, None otherwise
        """
        return self._backend.get_point_id(hash_prefix)

    def get_metadata(self, hash_prefix: str) -> Optional[Dict]:
        """Retrieve full metadata from hash prefix.

        Args:
            hash_prefix: 16-char hash prefix

        Returns:
            Dict with point_id, commit_hash, file_path, chunk_index, or None
        """
        return self._backend.get_metadata(hash_prefix)

    def delete_metadata(self, hash_prefix: str) -> None:
        """Delete metadata entry.

        Args:
            hash_prefix: 16-char hash prefix to delete
        """
        self._backend.delete_metadata(hash_prefix)

    def cleanup_stale_metadata(self, valid_hash_prefixes: Set[str]) -> int:
        """Remove metadata entries without corresponding vector files.

        Args:
            valid_hash_prefixes: Set of hash prefixes that have vector files

        Returns:
            Number of stale entries removed
        """
        return self._backend.cleanup_stale_metadata(valid_hash_prefixes)

    def count_entries(self) -> int:
        """Count total metadata entries.

        Returns:
            Number of entries in metadata database
        """
        return self._backend.count_entries()

    @classmethod
    def detect_format(cls, collection_path: Path) -> str:
        """Detect temporal collection format (v1 or v2).

        Bug #1313 Step 8: backend-aware. In PostgreSQL/cluster mode (registry
        factory set), temporal_metadata.db NEVER exists on disk -- the
        metadata lives in PostgreSQL. Detection there is path-local instead:
        presence of at least one ``vector_<16-hex>.json`` file (v1 cannot
        exist in a PG cluster since it postdates Story #669/#1313). CLI/solo
        behavior (no factory) is UNCHANGED: presence of temporal_metadata.db.

        Args:
            collection_path: Path to temporal collection directory

        Returns:
            "v2" if the active backend's v2 marker is present, "v1" otherwise
        """
        if get_temporal_metadata_backend_factory() is not None:
            return "v2" if cls._has_v2_vector_file(collection_path) else "v1"

        metadata_db_path = collection_path / cls.METADATA_DB_NAME

        if metadata_db_path.exists():
            return "v2"
        return "v1"

    _V2_VECTOR_FILENAME_RE = re.compile(r"^vector_[0-9a-f]{16}\.json$")

    @classmethod
    def _has_v2_vector_file(cls, collection_path: Path) -> bool:
        """Return True if collection_path contains a v2-format vector file.

        Path-local only (no DB round-trip): checks for a filename matching
        ``vector_<16-hex>.json``, the deterministic v2 naming scheme
        (Story #669) shared by every backend.
        """
        if not collection_path.is_dir():
            return False
        for entry in collection_path.iterdir():
            if entry.is_file() and cls._V2_VECTOR_FILENAME_RE.match(entry.name):
                return True
        return False

    @classmethod
    def handle_v1_format(cls, collection_path: Path) -> None:
        """Handle v1 format detection with graceful error.

        Args:
            collection_path: Path to temporal collection directory

        Raises:
            TemporalFormatError: Always raised with clear re-index instructions
        """
        format_version = cls.detect_format(collection_path)

        if format_version == "v1":
            error_message = (
                f"Legacy temporal index format (v1) detected at {collection_path}\n"
                f"Re-index required. Run: cidx index --index-commits --reconcile"
            )
            logger.error(error_message)
            raise TemporalFormatError(error_message)

    @classmethod
    def is_temporal_collection(cls, collection_name: str) -> bool:
        """Check if collection name is a temporal collection (legacy or provider-aware).

        Args:
            collection_name: Collection name to check

        Returns:
            True if this is a temporal collection
        """
        from code_indexer.services.temporal.temporal_collection_naming import (
            is_temporal_collection as _is_temporal,
        )

        return bool(_is_temporal(collection_name))
