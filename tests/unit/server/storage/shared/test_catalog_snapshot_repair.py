"""Retained catalog snapshots owned by the service user converge to owner +
group only for memories and the index tree; snapshots owned by another user
are counted once and left unchanged."""

import logging
import os
import stat
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.storage.shared.catalog_tree_permissions import (
    repair_retained_catalog_snapshots,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _retained(base: Path, namespace: str, name: str) -> Path:
    snapshot = base / ".versioned" / namespace / name
    for sub in ("memories", ".code-indexer/index"):
        (snapshot / sub).mkdir(parents=True)
    (snapshot / "memories" / "mem-0001.md").write_text("note")
    (snapshot / ".code-indexer" / "index" / "chunks.db").write_bytes(b"rows")
    for path in [snapshot, *snapshot.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return snapshot


def _private_entries_with_other_bits(snapshot: Path) -> List[Path]:
    roots = [snapshot / "memories", snapshot / ".code-indexer"]
    entries = [p for root in roots for p in [root, *root.rglob("*")]]
    return [p for p in entries if not p.is_symlink() and _mode(p) & stat.S_IRWXO]


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "versioned_base"


@pytest.fixture
def manager(base: Path) -> VersionedSnapshotManager:
    return VersionedSnapshotManager(
        clone_backend=LocalCloneBackend(versioned_base=str(base)),
        versioned_base=str(base),
    )


def test_retained_catalog_snapshots_converge(
    base: Path, manager: VersionedSnapshotManager
) -> None:
    snapshots = [_retained(base, "cidx-meta", f"v_170000000{i}") for i in (1, 2)]

    repair_retained_catalog_snapshots(manager)

    for snapshot in snapshots:
        assert _private_entries_with_other_bits(snapshot) == []
        assert _mode(snapshot / "memories" / "mem-0001.md") == 0o640


def test_close_catalog_private_tree_is_the_only_tree_closer(base: Path) -> None:
    from code_indexer.server.storage.shared import catalog_tree_permissions

    snapshot = _retained(base, "cidx-meta", "v_1700000001")

    catalog_tree_permissions.close_catalog_private_tree(str(snapshot))

    assert _private_entries_with_other_bits(snapshot) == []
    assert not hasattr(catalog_tree_permissions, "restrict_catalog_tree")


def test_retained_snapshot_with_dangling_symlink_converges(
    base: Path, manager: VersionedSnapshotManager, caplog: pytest.LogCaptureFixture
) -> None:
    snapshot = _retained(base, "cidx-meta", "v_1700000001")
    os.symlink(base / "no-such-target", snapshot / "memories" / "dangling.md")

    with caplog.at_level(logging.WARNING):
        repaired = repair_retained_catalog_snapshots(manager)

    assert repaired == 1
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert _private_entries_with_other_bits(snapshot) == []


def test_repair_is_idempotent(base: Path, manager: VersionedSnapshotManager) -> None:
    snapshot = _retained(base, "cidx-meta", "v_1700000001")
    repair_retained_catalog_snapshots(manager)
    before = {p: _mode(p) for p in snapshot.rglob("*")}

    repair_retained_catalog_snapshots(manager)

    assert {p: _mode(p) for p in snapshot.rglob("*")} == before


def test_other_repo_snapshots_are_untouched(
    base: Path, manager: VersionedSnapshotManager
) -> None:
    other = _retained(base, "example-repo", "v_1700000001")

    repair_retained_catalog_snapshots(manager)

    assert _mode(other / "memories" / "mem-0001.md") == 0o644


def test_failing_snapshot_does_not_stop_the_others(
    base: Path, manager: VersionedSnapshotManager, caplog: pytest.LogCaptureFixture
) -> None:
    broken = _retained(base, "cidx-meta", "v_1700000001")
    healthy = _retained(base, "cidx-meta", "v_1700000002")
    sealed = broken / ".code-indexer" / "sealed"
    sealed.mkdir()
    os.chmod(sealed, 0)
    try:
        with caplog.at_level(logging.WARNING):
            repair_retained_catalog_snapshots(manager)
    finally:
        os.chmod(sealed, 0o700)

    assert _private_entries_with_other_bits(healthy) == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "CatalogPermissionError" in warnings[0].getMessage()


def test_snapshots_of_another_owner_are_counted_once(
    base: Path,
    manager: VersionedSnapshotManager,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [_retained(base, "cidx-meta", f"v_170000000{i}") for i in (1, 2)]
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

    with caplog.at_level(logging.INFO):
        repair_retained_catalog_snapshots(manager)

    for snapshot in snapshots:
        assert _mode(snapshot / "memories" / "mem-0001.md") == 0o644
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len([m for m in infos if "another user" in m]) == 1
    assert any(" 2 " in f" {m} " for m in infos if "another user" in m)
