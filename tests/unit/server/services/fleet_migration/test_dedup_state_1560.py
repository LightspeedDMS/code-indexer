"""
Unit tests for Story #1560's dedup_state.py wrapper module (AC7/AC22/AC23).

Real SQLite backend for success-path coverage. The failure-path tests use
a minimal test double (mirroring test_quarantine_1477.py's own established
_AlwaysFailingBackend/_AlwaysFailingWriteBackend/_AlwaysFailingResetBackend
convention for this exact scenario) -- this wrapper module's own job is
exception TRANSLATION, identical regardless of whether the underlying
failure came from a real closed SQLite connection or a fake raising
RuntimeError. Real-SQLite-backend coverage for the backend layer itself
lives in test_fleet_migration_dedup_state_1560.py.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.fleet_migration.dedup_state import (
    DedupStateUnavailableError,
    clear_dedup_state,
    get_dedup_state,
    list_dedup_states,
    record_dedup_outcome,
    sweep_pending_dedup_outcomes_for_candidate,
)
from code_indexer.server.services.fleet_migration.discovery import (
    FleetMigrationCandidate,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    read_pending_dedup_outcome,
    repair_duplicate_and_shifted_points,
)


class _FakeGoldenRepoManagerWithBackend:
    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


class _AlwaysFailingBackend:
    """Mirrors test_quarantine_1477.py's own _AlwaysFailingBackend
    convention -- a minimal double implementing only the methods under
    test, raising to simulate a persistent backend outage."""

    def record_dedup_outcome(self, golden_alias, **kwargs):
        raise RuntimeError("simulated persistent backend write outage")

    def get_dedup_state(self, golden_alias):
        raise RuntimeError("simulated persistent backend read outage")


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


class TestRecordAndReadDedupState:
    def test_record_then_get_round_trips(self, backend):
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        row = record_dedup_outcome(
            manager,
            "click",
            duplicate_groups=33,
            records_before=343604,
            records_deleted=43,
            winner_kept_groups=23,
            whole_group_deleted_groups=10,
            collection_total=343604,
        )
        assert row is not None
        assert row["duplicate_groups"] == 33

        state = get_dedup_state(manager, "click")
        assert state is not None
        assert state["records_deleted"] == 43

    def test_get_returns_none_when_never_recorded(self, backend):
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        assert get_dedup_state(manager, "never-had-a-duplicate") is None

    def test_list_includes_recorded_alias(self, backend):
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_dedup_outcome(
            manager,
            "click",
            duplicate_groups=1,
            records_before=10,
            records_deleted=1,
            winner_kept_groups=1,
            whole_group_deleted_groups=0,
            collection_total=10,
        )
        aliases = {row["golden_alias"] for row in list_dedup_states(manager)}
        assert aliases == {"click"}

    def test_clear_marks_cleared(self, backend):
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_dedup_outcome(
            manager,
            "click",
            duplicate_groups=1,
            records_before=10,
            records_deleted=1,
            winner_kept_groups=1,
            whole_group_deleted_groups=0,
            collection_total=10,
        )
        clear_dedup_state(manager, "click", "successful full re-index")
        state = get_dedup_state(manager, "click")
        assert state is not None
        assert state["cleared_at"] is not None


class TestBackendFailurePropagation:
    def test_record_propagates_dedup_state_unavailable(self):
        manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingBackend())
        with pytest.raises(DedupStateUnavailableError):
            record_dedup_outcome(
                manager,
                "click",
                duplicate_groups=1,
                records_before=10,
                records_deleted=1,
                winner_kept_groups=1,
                whole_group_deleted_groups=0,
                collection_total=10,
            )

    def test_get_propagates_dedup_state_unavailable(self):
        manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingBackend())
        with pytest.raises(DedupStateUnavailableError):
            get_dedup_state(manager, "click")

    def test_record_returns_none_when_no_backend_configured(self):
        class _NoBackendManager:
            pass

        assert (
            record_dedup_outcome(
                _NoBackendManager(),
                "click",
                duplicate_groups=1,
                records_before=10,
                records_deleted=1,
                winner_kept_groups=1,
                whole_group_deleted_groups=0,
                collection_total=10,
            )
            is None
        )


def _write_duplicate_record(collection_dir: Path, suffix: str) -> None:
    point_id = hashlib.md5(b"proj_sha256:sweep_0").hexdigest()
    record = {
        "id": point_id,
        "vector": [0.1, 0.2],
        "payload": {
            "unique_key": "proj_sha256:sweep_0",
            "line_start": 1,
            "line_end": 5,
        },
    }
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_candidate(tmp_path: Path, alias: str = "click") -> FleetMigrationCandidate:
    base_clone = tmp_path / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "coll",
                "vector_size": 2,
                "hnsw_index": {
                    "version": 1,
                    "vector_dim": 2,
                    "space": "cosine",
                    "vector_count": 0,
                    "id_mapping": {},
                },
            }
        )
    )
    return FleetMigrationCandidate(
        sort_key=alias,
        golden_alias=alias,
        base_clone_path=base_clone,
        index_path=index_path,
        semantic_collection_dirs=[collection_dir],
        temporal_namespaces=[],
        sister_root=tmp_path / "golden-repos",
        sister_alias_manager=AliasManager(str(tmp_path / "golden-repos" / "aliases")),
    )


class TestSweepPendingDedupOutcomesForCandidate:
    def test_sweeps_a_real_journal_and_persists_and_clears(self, tmp_path, backend):
        candidate = _make_candidate(tmp_path)
        collection_dir = candidate.semantic_collection_dirs[0]

        # Real duplicate fixture -> repair() writes a REAL crash-durable
        # journal as a side effect.
        _write_duplicate_record(collection_dir, "-a")
        _write_duplicate_record(collection_dir, "-b")
        point_id = hashlib.md5(b"proj_sha256:sweep_0").hexdigest()
        winner_path = next(collection_dir.rglob(f"vector_{point_id}.json"))
        IDIndexManager().save_index(collection_dir, {point_id: winner_path})
        repair_duplicate_and_shifted_points(collection_dir)
        assert read_pending_dedup_outcome(collection_dir) is not None

        manager = _FakeGoldenRepoManagerWithBackend(backend)
        sweep_pending_dedup_outcomes_for_candidate(manager, candidate)

        assert read_pending_dedup_outcome(collection_dir) is None
        state = get_dedup_state(manager, candidate.golden_alias)
        assert state is not None
        assert state["records_deleted"] == 1

    def test_no_op_when_no_journal_present(self, tmp_path, backend):
        candidate = _make_candidate(tmp_path)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        # Must not raise, must not create any state.
        sweep_pending_dedup_outcomes_for_candidate(manager, candidate)
        assert get_dedup_state(manager, candidate.golden_alias) is None

    def test_leaves_journal_in_place_on_backend_failure(self, tmp_path):
        candidate = _make_candidate(tmp_path)
        collection_dir = candidate.semantic_collection_dirs[0]
        _write_duplicate_record(collection_dir, "-a")
        _write_duplicate_record(collection_dir, "-b")
        point_id = hashlib.md5(b"proj_sha256:sweep_0").hexdigest()
        winner_path = next(collection_dir.rglob(f"vector_{point_id}.json"))
        IDIndexManager().save_index(collection_dir, {point_id: winner_path})
        repair_duplicate_and_shifted_points(collection_dir)
        assert read_pending_dedup_outcome(collection_dir) is not None

        manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingBackend())
        with pytest.raises(DedupStateUnavailableError):
            sweep_pending_dedup_outcomes_for_candidate(manager, candidate)

        # AC23: the journal must survive a backend write failure.
        assert read_pending_dedup_outcome(collection_dir) is not None
