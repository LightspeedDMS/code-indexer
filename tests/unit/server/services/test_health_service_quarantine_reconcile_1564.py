"""Unit tests for Bug #1564: HealthCheckService must re-validate and
reap stale fleet-migration quarantine rows BEFORE reporting them on
/health -- otherwise a stale row (e.g. a manually-repaired
UNRECOVERABLE_FAILURE_CAUSE row, or one for a golden repo that no
longer exists) produces a permanent false DEGRADED /health entry.

This wires `quarantine.reconcile_stale_quarantine_rows()`
(tests/unit/server/services/test_fleet_migration_quarantine_reap_1564.py
and test_fleet_migration_quarantine_health_evidence_1564.py cover that
function directly) into
`HealthCheckService._collect_fleet_migration_unrecoverable_failures()`.

Real SQLite backend + real on-disk collection directories + the real
`consolidate_collection_in_place()` engine -- `HealthCheckService`
itself holds no `golden_repo_manager` reference by design (a minimal,
self-contained /health surface), so these tests monkeypatch the
module-level `get_golden_repo_manager()` accessor
(`code_indexer.server.repositories.golden_repo_manager`) to point at a
fake manager whose `_sqlite_backend` is the SAME SQLite file
`HealthCheckService.database_url` reads -- mirroring how the real
server shares one backend between `GoldenRepoManager` and /health.
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
    UNRECOVERABLE_FAILURE_CAUSE,
    get_failure_state,
    record_unrecoverable_corruption,
)
from code_indexer.server.services.health_service import HealthCheckService
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


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield os.path.join(temp_dir, "cidx_server.db")


@pytest.fixture
def sqlite_backend(db_path):
    be = GoldenRepoMetadataSqliteBackend(db_path)
    be.ensure_table_exists()
    try:
        yield be
    finally:
        be.close()


def _make_health_service(db_path: str) -> HealthCheckService:
    service = HealthCheckService()
    service.database_url = f"sqlite:///{db_path}"
    return service


def _patch_golden_repo_manager(monkeypatch, manager) -> None:
    monkeypatch.setattr(
        "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
        lambda: manager,
    )


class TestHealthyRepoIsCleared:
    def test_unrecoverable_row_clears_and_disappears_from_failure_reasons(
        self, tmp_path, db_path, sqlite_backend, monkeypatch
    ):
        base_clone = _make_repo_with_collection(tmp_path, "langfuse")
        _consolidate(base_clone)
        manager = _FakeGoldenRepoManager(sqlite_backend, repos={"langfuse": base_clone})
        record_unrecoverable_corruption(manager, "langfuse", "chunks.db was corrupt")
        _patch_golden_repo_manager(monkeypatch, manager)

        service = _make_health_service(db_path)
        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []
        assert get_failure_state(manager, "langfuse") is None


class TestOrphanedAliasIsReaped:
    def test_row_for_deleted_golden_repo_is_reaped_and_not_reported(
        self, db_path, sqlite_backend, monkeypatch
    ):
        manager = _FakeGoldenRepoManager(sqlite_backend, repos={})
        record_unrecoverable_corruption(manager, "evolution", "corrupt detail")
        _patch_golden_repo_manager(monkeypatch, manager)

        service = _make_health_service(db_path)
        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is False
        assert reasons == []
        assert get_failure_state(manager, "evolution") is None


class TestGenuinelyBrokenStaysReported:
    def test_still_sharded_json_row_stays_degraded(
        self, tmp_path, db_path, sqlite_backend, monkeypatch
    ):
        base_clone = _make_repo_with_collection(tmp_path, "broken-repo")
        # Deliberately never consolidated.
        manager = _FakeGoldenRepoManager(
            sqlite_backend, repos={"broken-repo": base_clone}
        )
        record_unrecoverable_corruption(manager, "broken-repo", "corrupt detail")
        _patch_golden_repo_manager(monkeypatch, manager)

        service = _make_health_service(db_path)
        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is True
        assert has_error is False
        assert any("broken-repo" in reason for reason in reasons)

    def test_locked_chunk_store_does_not_crash_and_stays_reported(
        self, tmp_path, db_path, sqlite_backend, monkeypatch
    ):
        base_clone = _make_repo_with_collection(tmp_path, "locked-repo")
        _consolidate(base_clone)
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        chunks_db_path = collection_dir / "chunks.db"

        manager = _FakeGoldenRepoManager(
            sqlite_backend, repos={"locked-repo": base_clone}
        )
        record_unrecoverable_corruption(manager, "locked-repo", "corrupt detail")
        _patch_golden_repo_manager(monkeypatch, manager)

        service = _make_health_service(db_path)

        locker_conn = sqlite3.connect(str(chunks_db_path))
        locker_conn.execute("BEGIN EXCLUSIVE")
        try:
            has_warning, has_error, reasons = (
                service._collect_fleet_migration_unrecoverable_failures()
            )
        finally:
            locker_conn.rollback()
            locker_conn.close()

        assert has_warning is True
        assert has_error is False
        assert any("locked-repo" in reason for reason in reasons)
        state = get_failure_state(manager, "locked-repo")
        assert state is not None
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE


class TestReconciliationFailsOpenWithoutGoldenRepoManager:
    def test_missing_golden_repo_manager_leaves_state_untouched_and_does_not_crash(
        self, db_path, sqlite_backend, monkeypatch
    ):
        manager_for_write_only = _FakeGoldenRepoManager(sqlite_backend, repos={})
        record_unrecoverable_corruption(
            manager_for_write_only, "evolution", "corrupt detail"
        )

        def _raise_unavailable():
            raise RuntimeError("golden_repo_manager not initialized")

        monkeypatch.setattr(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            _raise_unavailable,
        )

        service = _make_health_service(db_path)
        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is True
        assert any("evolution" in reason for reason in reasons)
        assert get_failure_state(manager_for_write_only, "evolution") is not None
