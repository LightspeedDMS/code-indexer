"""Bug #2022 Gap 4: a refresh whose ``cidx index`` fails on a fatal
chunk-store error must self-heal instead of failing identically forever.

Real components throughout: the real ``cidx index`` child (resolved from
PATH, exactly as ``_index_source`` spawns it), a real CHUNKS_DB collection
corrupted on disk, a real published snapshot under ``.versioned/``, the real
reflink restore (``LocalCloneBackend``), the real SQLite global registry,
and a real metadata store (SQLite always; PostgreSQL when
``TEST_POSTGRES_DSN`` is set).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from tests.utils.fatal_chunk_store_fixtures import (
    DUMMY_VOYAGE_KEY,
    corrupt_btree_pages,
    install_cidx_shim,
    quick_check_ok,
    read_metadata_marker,
)
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    RecordingJobManager,
    build_harness,
    sha256_of,
    typed_kinds,
)


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture
def cidx_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim_dir = install_cidx_shim(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VOYAGE_API_KEY", DUMMY_VOYAGE_KEY)


def _refresh_expecting_failure(harness: Harness) -> BaseException:
    with pytest.raises(Exception) as raised:
        harness.scheduler._execute_refresh(ALIAS)
    return raised.value


@pytest.mark.usefixtures("cidx_on_path")
class TestCorruptSourceSelfHeal:
    def test_restored_struck_then_next_cycle_publishes(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        snapshot_sha_before = sha256_of(harness.snapshot_db())
        corrupt_btree_pages(harness.source_db)

        error = _refresh_expecting_failure(harness)

        # (b) restored from the published snapshot and verified healthy,
        # metadata restored alongside, the snapshot itself untouched.
        assert quick_check_ok(harness.source_db), "source chunks.db not restored"
        assert read_metadata_marker(harness.source) == "snapshot-good"
        assert sha256_of(harness.snapshot_db()) == snapshot_sha_before
        # (a) the cycle failed with the typed error, never published.
        assert "corruption" in typed_kinds(error), repr(error)
        assert harness.scheduler.alias_manager.read_alias(ALIAS) == str(
            harness.snapshot
        )
        # (c) a persisted strike.
        assert harness.strikes() == 1

        # (d) next cycle reconciles against the restored store and publishes.
        # The embedding child is replaced here only because it needs a
        # provider; the store under it is the real restored chunks.db.
        with patch.object(harness.scheduler, "_index_source"):
            result = harness.scheduler._execute_refresh(ALIAS)

        assert result["success"] is True, result
        new_target = harness.scheduler.alias_manager.read_alias(ALIAS)
        assert new_target is not None and new_target != str(harness.snapshot)
        assert ".versioned" in new_target
        assert harness.strikes() == 0

    def test_missing_snapshot_fails_loudly_with_strike_and_no_restore(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="none")
        corrupt_btree_pages(harness.source_db)
        corrupt_sha = sha256_of(harness.source_db)

        error = _refresh_expecting_failure(harness)

        assert harness.strikes() == 1
        assert sha256_of(harness.source_db) == corrupt_sha, "nothing to restore from"
        assert "corruption" in typed_kinds(error), repr(error)

    def test_corrupt_snapshot_is_never_used_as_restore_source(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="corrupt")
        corrupt_btree_pages(harness.source_db)
        corrupt_sha = sha256_of(harness.source_db)
        snapshot_sha = sha256_of(harness.snapshot_db())

        _refresh_expecting_failure(harness)

        assert harness.strikes() == 1
        assert sha256_of(harness.source_db) == corrupt_sha
        assert sha256_of(harness.snapshot_db()) == snapshot_sha

    def test_quarantine_after_three_strikes_stops_indexing(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="corrupt")
        corrupt_btree_pages(harness.source_db)

        for _ in range(3):
            _refresh_expecting_failure(harness)
        assert harness.strikes() == 3

        with patch.object(harness.scheduler, "_index_source") as index_source:
            result = harness.scheduler._execute_refresh(ALIAS)

        assert result.get("skipped") == "integrity_quarantined", result
        index_source.assert_not_called()


@pytest.mark.usefixtures("cidx_on_path")
class TestEnvironmentFailureIsNotCorruption:
    @pytest.fixture(autouse=True)
    def _not_root(self) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores file modes; cannot produce a permission failure")

    def test_unreadable_store_is_never_restored_and_backs_off(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        sha_before = sha256_of(harness.source_db)
        harness.source_db.chmod(0o000)
        try:
            error = _refresh_expecting_failure(harness)
        finally:
            harness.source_db.chmod(0o644)

        assert "environment" in typed_kinds(error), repr(error)
        assert sha256_of(harness.source_db) == sha_before, (
            "restored over non-corruption"
        )
        assert read_metadata_marker(harness.source) == "source-new"
        assert harness.strikes() == 0

        jobs = RecordingJobManager()
        harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(ALIAS)
        assert jobs.submitted == [], "a backed-off alias was re-submitted"

    def test_verified_success_clears_the_backoff(
        self, tmp_path: Path, metadata: Any
    ) -> None:
        harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
        harness.source_db.chmod(0o000)
        try:
            _refresh_expecting_failure(harness)
        finally:
            harness.source_db.chmod(0o644)
        jobs = RecordingJobManager()
        harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(ALIAS)
        assert jobs.submitted == []

        harness.scheduler.background_job_manager = None
        with patch.object(harness.scheduler, "_index_source"):
            result = harness.scheduler._execute_refresh(ALIAS)
        assert result["success"] is True, result

        harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
        harness.scheduler.trigger_refresh_for_repo(ALIAS)
        assert jobs.submitted == [ALIAS]
