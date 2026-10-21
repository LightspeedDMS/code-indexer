"""Non-git local repository change detection measures every completed write.

A catalog without a git repository (``local://``) is refreshed only on a
trigger, and ``RefreshScheduler._has_local_changes`` decides whether the
trigger re-indexes. It must report a change for every write the published
index does not hold:

* a file written while the published run was indexing (after it listed the
  repository, before its snapshot was named), and
* a file deleted since the published snapshot.

Real filesystem, real scheduler; no snapshot manager is wired, so the
scheduler discovers snapshots under ``golden_repos_dir/.versioned``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

ALIAS = "cidx-meta-global"
LISTING_STARTED = 1_800_000_000.0
SNAPSHOT_TS = int(LISTING_STARTED) + 10


@pytest.fixture
def golden_dir(tmp_path: Path) -> Path:
    golden = tmp_path / "golden-repos"
    golden.mkdir()
    return golden


@pytest.fixture
def scheduler(tmp_path: Path, golden_dir: Path) -> RefreshScheduler:
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    return RefreshScheduler(
        golden_repos_dir=str(golden_dir),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
    )


def _write(path: Path, text: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _publish_snapshot(golden_dir: Path, source: Path, listing_started: float) -> Path:
    """A published snapshot: a copy of *source* named after SNAPSHOT_TS whose
    indexing metadata records the time its run listed the repository."""
    snapshot = golden_dir / ".versioned" / "cidx-meta" / f"v_{SNAPSHOT_TS}"
    for item in source.rglob("*"):
        if item.is_file():
            target = snapshot / item.relative_to(source)
            _write(target, item.read_text("utf-8"), item.stat().st_mtime)
    meta = snapshot / ".code-indexer" / "metadata.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps({"status": "completed", "last_index_timestamp": listing_started}),
        encoding="utf-8",
    )
    return snapshot


@pytest.fixture
def source(golden_dir: Path) -> Path:
    catalog = golden_dir / "cidx-meta"
    _write(catalog / "alpha.md", "alpha", LISTING_STARTED - 100)
    _write(catalog / "memories" / "kept.md", "kept", LISTING_STARTED - 50)
    return catalog


class TestWriteDuringPublishedRun:
    def test_file_written_after_listing_before_snapshot_is_a_change(
        self, scheduler: RefreshScheduler, golden_dir: Path, source: Path
    ) -> None:
        # The memory lands while the published run indexes: after its listing
        # started, before the snapshot is named. The run never saw it.
        _write(source / "memories" / "new.md", "new", LISTING_STARTED + 5)
        _publish_snapshot(golden_dir, source, LISTING_STARTED)

        assert scheduler._has_local_changes(str(source), ALIAS) is True


class TestDeletionSincePublishedSnapshot:
    def test_file_deleted_since_snapshot_is_a_change(
        self, scheduler: RefreshScheduler, golden_dir: Path, source: Path
    ) -> None:
        # A memory the published index holds is deleted: no remaining file
        # moves its mtime, yet the index still serves the deleted memory.
        _write(source / "memories" / "gone.md", "gone", LISTING_STARTED - 10)
        _publish_snapshot(golden_dir, source, LISTING_STARTED)
        (source / "memories" / "gone.md").unlink()

        assert scheduler._has_local_changes(str(source), ALIAS) is True

    def test_unchanged_source_is_not_a_change(
        self, scheduler: RefreshScheduler, golden_dir: Path, source: Path
    ) -> None:
        _publish_snapshot(golden_dir, source, LISTING_STARTED)

        assert scheduler._has_local_changes(str(source), ALIAS) is False
