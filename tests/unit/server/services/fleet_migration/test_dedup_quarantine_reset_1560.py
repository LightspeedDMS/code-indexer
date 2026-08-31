"""
Unit tests for Story #1560 AC12: an explicit, durable pre-attempt reset
for a repo already quarantined by a duplicate-point-id cause.

Design note: there is no dedicated "duplicate-point-id" failure_cause
constant. Per the story's own AC32 real-staging example ("quarantine
state at start: consecutive_failure_count=3, failure_cause='generic'"),
a duplicate-caused quarantine is recorded as ordinary
GENERIC_FAILURE_CAUSE -- prior to this story a duplicate point_id simply
raised DuplicateSourceIdError like any other exception, with no
cause-specific tag. The reset therefore fires because the collection
CURRENTLY has a duplicate (checked directly, read-only), never because
of a cause label.

Mirrors test_quarantine_1477.py's exact fixture conventions -- real
SQLite backend, real on-disk collection directories, no mocking of the
module under test.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.fleet_migration.discovery import (
    FleetMigrationCandidate,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    DISK_HEADROOM_FAILURE_CAUSE,
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
    GENERIC_FAILURE_CAUSE,
    QuarantineStateUnavailableError,
    UNRECOVERABLE_FAILURE_CAUSE,
    get_failure_state,
    record_migration_failure,
    record_unrecoverable_corruption,
    reset_duplicate_caused_quarantine_if_resolved,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


def _quarantine_with_disk_headroom_cause(backend, alias: str, count: int) -> None:
    """Quarantine `alias` for `count` consecutive failures, recorded
    with DISK_HEADROOM_FAILURE_CAUSE -- Codex finding F5: this cause is
    unrelated to duplicate-point-id resolution and must NOT be unblocked
    by reset_duplicate_caused_quarantine_if_resolved()."""
    for _ in range(count):
        record_migration_failure(
            _FakeGoldenRepoManagerWithBackend(backend),
            alias,
            "sig",
            failure_cause=DISK_HEADROOM_FAILURE_CAUSE,
        )


class _FakeGoldenRepoManagerWithBackend:
    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


class _AlwaysFailingReadBackend:
    def get_fleet_migration_failure_state(self, golden_alias: str):
        raise RuntimeError("simulated persistent backend outage")


def _write_duplicate_record(collection_dir: Path, suffix: str) -> None:
    point_id = hashlib.md5(b"proj_sha256:dup_0").hexdigest()
    record = {
        "id": point_id,
        "vector": [0.1, 0.2],
        "payload": {
            "unique_key": "proj_sha256:dup_0",
            "line_start": 1,
            "line_end": 5,
        },
    }
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_candidate(
    tmp_path: Path, alias: str = "click", *, with_duplicate: bool
) -> FleetMigrationCandidate:
    base_clone = tmp_path / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text("{}")
    if with_duplicate:
        _write_duplicate_record(collection_dir, "-a")
        _write_duplicate_record(collection_dir, "-b")

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


def _quarantine_with_generic_cause(backend, alias: str, count: int) -> None:
    """Quarantine `alias` for `count` consecutive failures, recorded
    with GENERIC_FAILURE_CAUSE -- the real-world shape a duplicate-
    point-id-caused failure takes (see module docstring)."""
    for _ in range(count):
        record_migration_failure(
            _FakeGoldenRepoManagerWithBackend(backend),
            alias,
            "sig",
            failure_cause=GENERIC_FAILURE_CAUSE,
        )


class TestResetFiresForDuplicateCausedQuarantine:
    def test_resets_when_quarantined_with_generic_cause_and_duplicate_present(
        self, tmp_path, backend
    ):
        candidate = _make_candidate(tmp_path, with_duplicate=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_generic_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is True
        assert get_failure_state(manager, candidate.golden_alias) is None

    def test_no_reset_when_below_threshold(self, tmp_path, backend):
        candidate = _make_candidate(tmp_path, with_duplicate=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_generic_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD - 1,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is False
        assert get_failure_state(manager, candidate.golden_alias) is not None

    def test_no_reset_when_cause_is_unrecoverable(self, tmp_path, backend):
        candidate = _make_candidate(tmp_path, with_duplicate=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_unrecoverable_corruption(manager, candidate.golden_alias, "corrupt")

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is False
        state = get_failure_state(manager, candidate.golden_alias)
        assert state is not None
        assert state["failure_cause"] == UNRECOVERABLE_FAILURE_CAUSE

    def test_no_reset_when_cause_is_disk_headroom(self, tmp_path, backend):
        """Codex finding F5: a disk-headroom-quarantined repo is unrelated
        to duplicate-point-id resolution -- the reset must not unblock it
        even though a real duplicate happens to also be present."""
        candidate = _make_candidate(tmp_path, with_duplicate=True)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_disk_headroom_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is False
        state = get_failure_state(manager, candidate.golden_alias)
        assert state is not None
        assert state["failure_cause"] == DISK_HEADROOM_FAILURE_CAUSE

    def test_no_reset_when_collection_has_no_duplicate(self, tmp_path, backend):
        candidate = _make_candidate(tmp_path, with_duplicate=False)
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        _quarantine_with_generic_cause(
            backend,
            candidate.golden_alias,
            FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
        )

        result = reset_duplicate_caused_quarantine_if_resolved(manager, candidate)

        assert result is False
        assert get_failure_state(manager, candidate.golden_alias) is not None

    def test_propagates_backend_read_failure(self, tmp_path):
        candidate = _make_candidate(tmp_path, with_duplicate=True)
        manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingReadBackend())

        with pytest.raises(QuarantineStateUnavailableError):
            reset_duplicate_caused_quarantine_if_resolved(manager, candidate)
