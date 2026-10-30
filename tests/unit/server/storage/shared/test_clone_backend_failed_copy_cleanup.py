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

import pytest

from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root reads a chmod 0 file, so cp would not fail part-way",
)

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
    from code_indexer.server.storage.shared.clone_backend import (
        CloneDestinationExistsError,
    )

    with pytest.raises(CloneDestinationExistsError):
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

    with pytest.raises(subprocess.CalledProcessError):
        manager.create_snapshot(REPO, str(source))

    assert sorted(p.name for p in _namespace(golden).iterdir()) == [f"v_{PREVIOUS_TS}"]
    assert [ts for _, ts in manager.list_snapshots(f"{REPO}-global")] == [PREVIOUS_TS]
