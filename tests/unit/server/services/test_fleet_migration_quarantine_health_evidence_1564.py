"""Unit tests for Bug #1564 (part 2/2): positive-evidence auto-clear,
genuinely-broken-stays-reported, and disk-headroom-not-duplicated
coverage for `quarantine.reconcile_stale_quarantine_rows()`.

See test_fleet_migration_quarantine_reap_1564.py for the orphaned-alias
reaping / fail-open coverage of the same function.

Real SQLite backend + real on-disk collection directories + the real
`consolidate_collection_in_place()` engine throughout -- no mocking of
the module under test or of the chunk-store primitives it re-uses.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.services.fleet_migration.quarantine import (
    DISK_HEADROOM_FAILURE_CAUSE,
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
    GENERIC_FAILURE_CAUSE,
    UNRECOVERABLE_FAILURE_CAUSE,
    get_failure_state,
    record_migration_failure,
    record_unrecoverable_corruption,
    reconcile_stale_quarantine_rows,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)


class _FakeGoldenRepoManager:
    def __init__(self, sqlite_backend, repos: Dict[str, Path]):
        self._sqlite_backend = sqlite_backend
        self._repos = repos

    def list_golden_repos(self) -> List[dict]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        try:
            yield be
        finally:
            be.close()


def _write_legacy_record(collection_dir: Path) -> None:
    point_id = hashlib.md5(b"proj_sha256_0").hexdigest()
    record = {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3, 0.4],
        "payload": {
            "path": "src/foo.py",
            "content": "chunk content 0",
            "language": "python",
            "project_id": "proj",
            "file_hash": "sha256",
            "chunk_index": 0,
            "total_chunks": 1,
            "line_start": 1,
            "line_end": 5,
            "point_id": point_id,
            "unique_key": "proj_sha256_0",
        },
    }
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_collection_meta(collection_dir: Path) -> None:
    (collection_dir / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "coll",
                "vector_size": 4,
                "hnsw_index": {
                    "version": 1,
                    "vector_dim": 4,
                    "space": "cosine",
                    "vector_count": 0,
                    "id_mapping": {},
                },
            }
        )
    )


def _make_repo_with_collection(tmp_path: Path, alias: str) -> Path:
    """Create a bare golden-repo clone with one semantic collection
    directory (legacy SHARDED_JSON layout, one real record) and return
    the base clone path."""
    base_clone = tmp_path / alias
    collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
    collection_dir.mkdir(parents=True)
    _write_collection_meta(collection_dir)
    _write_legacy_record(collection_dir)
    return base_clone


def _consolidate(base_clone: Path) -> None:
    collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
    result = consolidate_collection_in_place(collection_dir)
    assert result.status == "consolidated"


class TestClearsOnPositiveHealthEvidence:
    def test_unrecoverable_row_clears_when_chunk_store_now_reads_cleanly(
        self, tmp_path, backend
    ):
        base_clone = _make_repo_with_collection(tmp_path, "langfuse")
        _consolidate(base_clone)
        manager = _FakeGoldenRepoManager(backend, repos={"langfuse": base_clone})
        record_unrecoverable_corruption(manager, "langfuse", "chunks.db was corrupt")

        reconcile_stale_quarantine_rows(manager)

        assert get_failure_state(manager, "langfuse") is None

    def test_generic_row_clears_when_chunk_store_now_reads_cleanly(
        self, tmp_path, backend
    ):
        base_clone = _make_repo_with_collection(tmp_path, "cidx-meta")
        _consolidate(base_clone)
        manager = _FakeGoldenRepoManager(backend, repos={"cidx-meta": base_clone})
        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                manager, "cidx-meta", "sig", failure_cause=GENERIC_FAILURE_CAUSE
            )

        reconcile_stale_quarantine_rows(manager)

        assert get_failure_state(manager, "cidx-meta") is None


class TestGenuinelyBrokenStaysReported:
    def test_still_sharded_json_row_stays_quarantined(self, tmp_path, backend):
        base_clone = _make_repo_with_collection(tmp_path, "broken-repo")
        # Deliberately never consolidated -- still legacy SHARDED_JSON.
        manager = _FakeGoldenRepoManager(backend, repos={"broken-repo": base_clone})
        record_unrecoverable_corruption(manager, "broken-repo", "corrupt detail")

        reconcile_stale_quarantine_rows(manager)

        state = get_failure_state(manager, "broken-repo")
        assert state is not None
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE

    def test_locked_chunk_store_stays_quarantined_and_never_raises(
        self, tmp_path, backend
    ):
        base_clone = _make_repo_with_collection(tmp_path, "locked-repo")
        _consolidate(base_clone)
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        db_path = collection_dir / "chunks.db"

        manager = _FakeGoldenRepoManager(backend, repos={"locked-repo": base_clone})
        record_unrecoverable_corruption(manager, "locked-repo", "corrupt detail")

        locker_conn = sqlite3.connect(str(db_path))
        locker_conn.execute("BEGIN EXCLUSIVE")
        try:
            # Must never raise, even though the store is genuinely
            # unreadable right now.
            reconcile_stale_quarantine_rows(manager)
        finally:
            locker_conn.rollback()
            locker_conn.close()

        state = get_failure_state(manager, "locked-repo")
        assert state is not None
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE


class TestDoesNotDuplicateDiskHeadroomOracle:
    def test_disk_headroom_cause_is_left_untouched_even_when_chunk_store_is_healthy(
        self, tmp_path, backend
    ):
        """DISK_HEADROOM_FAILURE_CAUSE already has its own independent,
        correct auto-clear oracle inside is_quarantined()
        (`_disk_headroom_currently_sufficient`) -- this reconciliation
        function must never duplicate or interfere with it, even when
        the collection's chunk store happens to look healthy."""
        base_clone = _make_repo_with_collection(tmp_path, "disk-repo")
        _consolidate(base_clone)
        manager = _FakeGoldenRepoManager(backend, repos={"disk-repo": base_clone})
        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                manager, "disk-repo", "sig", failure_cause=DISK_HEADROOM_FAILURE_CAUSE
            )

        reconcile_stale_quarantine_rows(manager)

        state = get_failure_state(manager, "disk-repo")
        assert state is not None
        assert state["failure_cause"] == DISK_HEADROOM_FAILURE_CAUSE
