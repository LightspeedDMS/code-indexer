"""SQLite backend for temporal metadata storage (Bug #1313 Step 3).

Extracted VERBATIM from the original TemporalMetadataStore (Story #669) body
so CLI/solo behavior is byte-for-byte unchanged. This is the default backend
used whenever no PostgreSQL backend factory is registered (see
temporal_metadata_backend_registry.py) -- i.e. the CLI, daemon, and solo
server modes.

Satisfies the TemporalMetadataBackend Protocol (temporal_metadata_backend.py).
"""

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .temporal_metadata_store import generate_hash_prefix

logger = logging.getLogger(__name__)


class TemporalMetadataSqliteBackend:
    """SQLite-based metadata storage for temporal collections v2 format.

    Stores point_id-to-hash mappings with metadata:
    - hash_prefix: 16-char SHA256 prefix (used as filename)
    - point_id: Full point_id (original)
    - commit_hash: Git commit hash
    - file_path: File path from payload
    - chunk_index: Chunk index
    - created_at: Timestamp

    Schema:
        CREATE TABLE temporal_metadata (
            hash_prefix TEXT PRIMARY KEY,
            point_id TEXT NOT NULL UNIQUE,
            commit_hash TEXT,
            file_path TEXT,
            chunk_index INTEGER,
            created_at TEXT,
            format_version INTEGER DEFAULT 2
        );
    """

    METADATA_DB_NAME = "temporal_metadata.db"

    def __init__(self, collection_path: Path):
        """Initialize temporal metadata store.

        Args:
            collection_path: Path to the temporal collection directory
        """
        self.collection_path = collection_path
        self.db_path = collection_path / self.METADATA_DB_NAME
        self._init_database()

    def _init_database(self):
        """Initialize SQLite database with schema."""
        self.collection_path.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        try:
            # Bug #484 Fix: Enable WAL mode for concurrent access.
            # WAL (Write-Ahead Logging) allows multiple readers and one writer
            # to coexist without blocking. This is essential when TemporalIndexer
            # runs 8+ parallel threads all writing via save_metadata().
            conn.execute("PRAGMA journal_mode=WAL")
            # Bug #484 Fix: Set busy_timeout so writers wait instead of
            # immediately raising OperationalError: database is locked.
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()

            # Create table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS temporal_metadata (
                    hash_prefix TEXT PRIMARY KEY,
                    point_id TEXT NOT NULL UNIQUE,
                    commit_hash TEXT,
                    file_path TEXT,
                    chunk_index INTEGER,
                    created_at TEXT,
                    format_version INTEGER DEFAULT 2
                )
            """
            )

            # Create indexes for efficient queries
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_point_id
                ON temporal_metadata(point_id)
            """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_commit_hash
                ON temporal_metadata(commit_hash)
            """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_file_path
                ON temporal_metadata(file_path)
            """
            )

            conn.commit()
        finally:
            conn.close()

    def save_metadata(self, point_id: str, payload: Dict) -> str:
        """Save metadata for a point and return hash prefix.

        Args:
            point_id: Full point_id
            payload: Payload dict containing commit_hash, path, chunk_index

        Returns:
            16-char hash prefix (to be used as filename)
        """
        hash_prefix = generate_hash_prefix(point_id)

        # Extract metadata from payload with validation logging
        commit_hash = payload.get("commit_hash", "")
        if not commit_hash:
            logger.warning(f"Missing commit_hash in payload for point_id: {point_id}")

        file_path = payload.get("path", "")
        if not file_path and payload.get("type") != "commit_message":
            logger.warning(f"Missing path in payload for point_id: {point_id}")

        chunk_index = payload.get("chunk_index", 0)
        if "chunk_index" not in payload:
            logger.debug(f"chunk_index not in payload for {point_id}, defaulting to 0")

        created_at = datetime.now().isoformat()

        # Bug #484 Fix: Retry logic for concurrent write contention.
        # TemporalIndexer runs 8+ parallel threads; even with WAL mode and
        # busy_timeout, transient lock contention can occur.  Retry up to 3
        # times with exponential backoff before propagating the error.
        max_retries = 3
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            conn = sqlite3.connect(self.db_path)
            try:
                # Bug #484 Fix: Enable WAL mode and busy_timeout on every
                # connection so writers wait instead of failing immediately.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO temporal_metadata
                    (hash_prefix, point_id, commit_hash, file_path, chunk_index, created_at, format_version)
                    VALUES (?, ?, ?, ?, ?, ?, 2)
                """,
                    (
                        hash_prefix,
                        point_id,
                        commit_hash,
                        file_path,
                        chunk_index,
                        created_at,
                    ),
                )
                conn.commit()
                return hash_prefix
            except sqlite3.OperationalError as e:
                last_error = e
                logger.warning(
                    "save_metadata: attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    max_retries,
                    point_id,
                    e,
                )
                if attempt < max_retries - 1:
                    time.sleep(0.05 * (2**attempt))  # 50ms, 100ms backoff
            finally:
                conn.close()

        raise sqlite3.OperationalError(
            f"save_metadata failed after {max_retries} attempts for {point_id}: {last_error}"
        )

    def save_metadata_batch(self, rows: List[Tuple[str, Dict]]) -> List[str]:
        """Save metadata for multiple points in ONE transaction.

        Bug #1206 Fix 1: replaces N individual connect/commit/close cycles with a
        single connection opened once, all rows inserted via executemany, one
        commit, one close.  This eliminates the per-vector fsync bottleneck that
        serialised all 8 embed threads on the same SQLite WAL.

        Args:
            rows: List of (point_id, payload) tuples.

        Returns:
            List of 16-char hash prefixes in the same order as input rows.
        """
        if not rows:
            return []

        created_at = datetime.now().isoformat()
        params = []
        hash_prefixes = []
        for point_id, payload in rows:
            hash_prefix = generate_hash_prefix(point_id)
            hash_prefixes.append(hash_prefix)
            commit_hash = payload.get("commit_hash", "")
            file_path = payload.get("path", "")
            chunk_index = payload.get("chunk_index", 0)
            params.append(
                (hash_prefix, point_id, commit_hash, file_path, chunk_index, created_at)
            )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executemany(
                """
                INSERT OR REPLACE INTO temporal_metadata
                (hash_prefix, point_id, commit_hash, file_path, chunk_index, created_at, format_version)
                VALUES (?, ?, ?, ?, ?, ?, 2)
                """,
                params,
            )
            conn.commit()
        finally:
            conn.close()

        return hash_prefixes

    def checkpoint_wal(self) -> None:
        """Run a PASSIVE WAL checkpoint to bound WAL file growth.

        Bug #1206 Fix 1: call periodically (e.g. every commit batch) to prevent
        the WAL from growing unbounded (a 93 MB WAL was observed on a 36 MB DB).
        PASSIVE mode: checkpoints as many frames as possible without blocking
        readers or writers.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            conn.close()

    def get_point_id(self, hash_prefix: str) -> Optional[str]:
        """Retrieve point_id from hash prefix.

        Args:
            hash_prefix: 16-char hash prefix

        Returns:
            Full point_id if found, None otherwise
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT point_id FROM temporal_metadata
                WHERE hash_prefix = ?
            """,
                (hash_prefix,),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def get_metadata(self, hash_prefix: str) -> Optional[Dict]:
        """Retrieve full metadata from hash prefix.

        Args:
            hash_prefix: 16-char hash prefix

        Returns:
            Dict with point_id, commit_hash, file_path, chunk_index, or None
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT point_id, commit_hash, file_path, chunk_index, created_at
                FROM temporal_metadata
                WHERE hash_prefix = ?
            """,
                (hash_prefix,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "point_id": row[0],
                    "commit_hash": row[1],
                    "file_path": row[2],
                    "chunk_index": row[3],
                    "created_at": row[4],
                }
            return None
        finally:
            conn.close()

    def delete_metadata(self, hash_prefix: str) -> None:
        """Delete metadata entry.

        Args:
            hash_prefix: 16-char hash prefix to delete
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM temporal_metadata
                WHERE hash_prefix = ?
            """,
                (hash_prefix,),
            )
            conn.commit()
        finally:
            conn.close()

    def cleanup_stale_metadata(self, valid_hash_prefixes: Set[str]) -> int:
        """Remove metadata entries without corresponding vector files.

        Args:
            valid_hash_prefixes: Set of hash prefixes that have vector files

        Returns:
            Number of stale entries removed
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Get all hash prefixes in database
            cursor.execute("SELECT hash_prefix FROM temporal_metadata")
            all_prefixes = {row[0] for row in cursor.fetchall()}

            # Find stale entries (in DB but no vector file)
            stale_prefixes = all_prefixes - valid_hash_prefixes

            if stale_prefixes:
                placeholders = ",".join(["?"] * len(stale_prefixes))
                cursor.execute(
                    f"""
                    DELETE FROM temporal_metadata
                    WHERE hash_prefix IN ({placeholders})
                """,
                    list(stale_prefixes),
                )
                conn.commit()
                logger.info(f"Cleaned up {len(stale_prefixes)} stale metadata entries")

            return len(stale_prefixes)
        finally:
            conn.close()

    def count_entries(self) -> int:
        """Count total metadata entries.

        Returns:
            Number of entries in metadata database
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM temporal_metadata")
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
