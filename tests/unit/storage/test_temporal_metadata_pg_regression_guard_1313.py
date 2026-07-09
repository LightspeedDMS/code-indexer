"""Bug #1313 direct regression guard.

With the PostgreSQL backend factory installed (simulating cluster mode),
driving FilesystemVectorStore.upsert_points() on a temporal collection must:
  1. Create NO temporal_metadata.db file under the collection directory
     (proving the SQLite-WAL NFS bottleneck is bypassed).
  2. Route every (point_id, payload) row through the installed backend (proving
     the registry factory is actually wired into the write hot path, not just
     available as an orphan helper).

No real PostgreSQL is used here -- a stand-in fake backend (satisfying
TemporalMetadataBackend structurally) plays the role of
TemporalMetadataPostgresBackend, isolating this test from live infrastructure
while still exercising the REAL FilesystemVectorStore.upsert_points() code
path end-to-end (no mocking of the code under test itself).
"""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.temporal_metadata_backend_registry import (
    clear_temporal_metadata_backend_factory,
    set_temporal_metadata_backend_factory,
)


def _make_real_pg_pool_if_available():
    """Return a ConnectionPool connected to a real test database, or None.

    Mirrors test_temporal_metadata_postgres_backend.py's
    ``_make_pool_if_available`` -- gated by TEST_POSTGRES_DSN (skip when
    absent), consistent with the repo-wide live-PG test convention (e.g.
    test_logs_postgres_backend.py, test_migration_runner.py). The broad
    except-and-return-None here is intentional and matches that established
    pattern exactly: this helper feeds a ``pytest.mark.skipif`` predicate, so
    ANY failure to reach a real PostgreSQL (missing driver, no DSN, DB down)
    must uniformly resolve to "skip this real-PG test class", never raise
    during collection.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return None
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return None
    try:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        pool = ConnectionPool(dsn)
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return pool
    except Exception:
        return None


def _postgres_available() -> bool:
    """Cheap boolean availability check for ``@pytest.mark.skipif`` predicates.

    Bug #1313 round-2 rework (Codex non-blocking nit): unlike
    ``_make_real_pg_pool_if_available()`` (which returns a live pool for
    tests to actually use), this helper opens a probe pool purely to verify
    connectivity and closes it immediately before returning, so pytest
    collection doesn't leak a ``ConnectionPool`` that
    ``psycopg_pool.ConnectionPool.__del__`` warns about via
    ``PytestUnraisableExceptionWarning``.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return False
    try:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        pool = ConnectionPool(dsn)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        finally:
            pool.close()
    except Exception:
        return False


@pytest.mark.skipif(
    not _postgres_available(),
    reason="TEST_POSTGRES_DSN not set or PostgreSQL unavailable",
)
class TestUpsertPointsRoutesThroughRealPostgresBackend:
    """Bug #1313 review Finding 3: the fake-backend test above
    (_FakePostgresStandinBackend) proves the registry factory reaches
    FilesystemVectorStore.upsert_points() -- it does NOT prove rows land in
    real PostgreSQL. This class drives the SAME upsert_points() code path
    through the REAL TemporalMetadataPostgresBackend, backed by a REAL
    PostgreSQL connection pool (TEST_POSTGRES_DSN), and asserts persistence
    both via the backend's own API and via a raw SQL SELECT against the
    real table -- the single most important piece of evidence that the
    cluster-NFS-bottleneck fix works end-to-end against real infrastructure.
    """

    def test_no_sqlite_db_file_and_rows_persisted_in_real_postgres(self):
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )
        from code_indexer.services.temporal.temporal_collection_naming import (
            LEGACY_TEMPORAL_COLLECTION,
        )

        pool = _make_real_pg_pool_if_available()
        assert pool is not None

        collection_keys_used: List[str] = []

        def _real_pg_factory(collection_path: Path) -> TemporalMetadataPostgresBackend:
            # Mirrors the EXACT collection_key derivation in
            # server/startup/lifespan.py's production factory wiring.
            collection_key = hashlib.sha256(str(collection_path).encode()).hexdigest()[
                :32
            ]
            collection_keys_used.append(collection_key)
            return TemporalMetadataPostgresBackend(pool, collection_key=collection_key)

        try:
            set_temporal_metadata_backend_factory(_real_pg_factory)

            with tempfile.TemporaryDirectory() as tmpdir:
                index_path = Path(tmpdir) / ".code-indexer" / "index"
                index_path.mkdir(parents=True, exist_ok=True)
                store = FilesystemVectorStore(base_path=index_path)

                collection_name = LEGACY_TEMPORAL_COLLECTION
                store.create_collection(collection_name, vector_size=1024)

                points = _make_temporal_points(7, commit_hash="regr1313realpg")
                store.upsert_points(collection_name=collection_name, points=points)

                collection_path = index_path / collection_name
                db_path = collection_path / "temporal_metadata.db"

                assert not db_path.exists(), (
                    "Bug #1313: temporal_metadata.db must NOT be created when "
                    "the REAL PostgreSQL backend factory is installed"
                )

                assert len(collection_keys_used) == 1
                collection_key = collection_keys_used[0]

                # (a) Verify via the backend's own API.
                backend = TemporalMetadataPostgresBackend(
                    pool, collection_key=collection_key
                )
                assert backend.count_entries() == 7

                # (b) Verify via RAW SQL against the real table (extra rigor,
                # independent of the backend code under test).
                with pool.connection() as conn:
                    rows = conn.execute(
                        "SELECT point_id, chunk_index FROM temporal_metadata "
                        "WHERE collection_key = %s ORDER BY chunk_index",
                        (collection_key,),
                    ).fetchall()

                assert len(rows) == 7
                expected_point_ids = {p["id"] for p in points}
                actual_point_ids = {row[0] for row in rows}
                assert actual_point_ids == expected_point_ids
                assert [row[1] for row in rows] == list(range(7))
        finally:
            clear_temporal_metadata_backend_factory()
            with pool.connection() as conn:
                for collection_key in collection_keys_used:
                    conn.execute(
                        "DELETE FROM temporal_metadata WHERE collection_key = %s",
                        (collection_key,),
                    )
                conn.commit()


class _FakePostgresStandinBackend:
    """Minimal in-memory stand-in satisfying TemporalMetadataBackend, used to
    prove the registry-factory wiring reaches upsert_points without requiring
    a live PostgreSQL connection in this test environment."""

    def __init__(self, collection_path: Path):
        self.collection_path = collection_path
        self._rows: Dict[str, Tuple[str, Dict]] = {}

    def save_metadata_batch(self, rows: List[Tuple[str, Dict]]) -> List[str]:
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix

        prefixes = []
        for point_id, payload in rows:
            hash_prefix = generate_hash_prefix(point_id)
            self._rows[hash_prefix] = (point_id, payload)
            prefixes.append(hash_prefix)
        return prefixes

    def save_metadata(self, point_id: str, payload: Dict) -> str:
        return self.save_metadata_batch([(point_id, payload)])[0]

    def checkpoint_wal(self) -> None:
        pass

    def get_point_id(self, hash_prefix: str) -> Optional[str]:
        entry = self._rows.get(hash_prefix)
        return entry[0] if entry else None

    def get_metadata(self, hash_prefix: str) -> Optional[Dict]:
        entry = self._rows.get(hash_prefix)
        if entry is None:
            return None
        return {"point_id": entry[0], **entry[1]}

    def delete_metadata(self, hash_prefix: str) -> None:
        self._rows.pop(hash_prefix, None)

    def cleanup_stale_metadata(self, valid_hash_prefixes: Set[str]) -> int:
        stale = set(self._rows) - valid_hash_prefixes
        for prefix in stale:
            del self._rows[prefix]
        return len(stale)

    def count_entries(self) -> int:
        return len(self._rows)


def _make_temporal_points(n: int, commit_hash: str = "regr1313") -> List[dict]:
    return [
        {
            "id": f"{commit_hash}:src/file{i}.py:{i}",
            "vector": list(np.zeros(1024, dtype=float)),
            "payload": {
                "type": "commit_diff",
                "commit_hash": commit_hash,
                "path": f"src/file{i}.py",
                "chunk_index": i,
            },
            "chunk_text": f"def func_{i}(): pass",
        }
        for i in range(n)
    ]


class TestUpsertPointsRoutesThroughRegisteredPgBackend:
    def test_no_sqlite_db_file_created_when_factory_installed(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            LEGACY_TEMPORAL_COLLECTION,
        )

        created_backends: List[_FakePostgresStandinBackend] = []

        def _factory(collection_path: Path) -> _FakePostgresStandinBackend:
            backend = _FakePostgresStandinBackend(collection_path)
            created_backends.append(backend)
            return backend

        try:
            set_temporal_metadata_backend_factory(_factory)

            with tempfile.TemporaryDirectory() as tmpdir:
                index_path = Path(tmpdir) / ".code-indexer" / "index"
                index_path.mkdir(parents=True, exist_ok=True)
                store = FilesystemVectorStore(base_path=index_path)

                collection_name = LEGACY_TEMPORAL_COLLECTION
                store.create_collection(collection_name, vector_size=1024)

                points = _make_temporal_points(10)
                store.upsert_points(collection_name=collection_name, points=points)

                collection_path = index_path / collection_name
                db_path = collection_path / "temporal_metadata.db"

                assert not db_path.exists(), (
                    "Bug #1313: temporal_metadata.db must NOT be created when "
                    "the PostgreSQL backend factory is installed"
                )

                assert len(created_backends) == 1
                assert created_backends[0].count_entries() == 10, (
                    "All 10 rows must have landed in the registered backend, "
                    "proving upsert_points routes through it end-to-end"
                )
        finally:
            clear_temporal_metadata_backend_factory()
