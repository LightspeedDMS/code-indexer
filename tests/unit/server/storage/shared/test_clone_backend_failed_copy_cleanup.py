"""A snapshot copy that fails part-way leaves no partial ``v_<ts>`` behind.

``LocalCloneBackend.create_clone`` runs ``cp --reflink=auto -a`` into a fresh
destination; when ``cp`` fails mid-copy the half-copied directory used to stay
on disk, and ``list_snapshots`` then treated it as the newest snapshot (the
refresh's change-check baseline). The clone backend removes only the
destination this call created -- never one that existed before it, never a
published snapshot.

Real ``cp``, real filesystem: an unreadable source file makes ``cp`` fail after
it created the destination and copied the readable files.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

REPO = "cidx-meta"
PREVIOUS_TS = 1_700_000_000


@pytest.fixture
def source(tmp_path: Path) -> Iterator[Path]:
    src = tmp_path / "golden-repos" / REPO
    src.mkdir(parents=True)
    (src / "readable.md").write_text("ok", encoding="utf-8")
    unreadable = src / "unreadable.md"
    unreadable.write_text("secret", encoding="utf-8")
    os.chmod(unreadable, 0)
    try:
        yield src
    finally:
        os.chmod(unreadable, 0o644)


def _namespace(golden: Path) -> Path:
    return golden / ".versioned" / REPO


def test_failed_copy_removes_partial_destination(source: Path) -> None:
    golden = source.parent
    backend = LocalCloneBackend(versioned_base=str(golden))

    with pytest.raises(subprocess.CalledProcessError):
        backend.create_clone(str(source), REPO, "v_1800000000")

    assert not (_namespace(golden) / "v_1800000000").exists()


def test_failed_copy_never_removes_a_preexisting_destination(source: Path) -> None:
    golden = source.parent
    existing = _namespace(golden) / "v_1800000000"
    existing.mkdir(parents=True)
    (existing / "published.md").write_text("published", encoding="utf-8")
    backend = LocalCloneBackend(versioned_base=str(golden))

    with pytest.raises(subprocess.CalledProcessError):
        backend.create_clone(str(source), REPO, "v_1800000000")

    assert (existing / "published.md").read_text(encoding="utf-8") == "published"


def test_change_check_baseline_stays_on_the_previous_good_snapshot(
    source: Path, tmp_path: Path
) -> None:
    golden = source.parent
    previous = _namespace(golden) / f"v_{PREVIOUS_TS}"
    previous.mkdir(parents=True)
    manager = VersionedSnapshotManager(
        versioned_base=str(golden),
        clone_backend=LocalCloneBackend(versioned_base=str(golden)),
    )
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
        snapshot_manager=manager,
    )

    with pytest.raises(subprocess.CalledProcessError):
        manager.create_snapshot(REPO, str(source))

    assert sorted(p.name for p in _namespace(golden).iterdir()) == [f"v_{PREVIOUS_TS}"]
    assert scheduler._latest_versioned_timestamp(f"{REPO}-global") == PREVIOUS_TS
