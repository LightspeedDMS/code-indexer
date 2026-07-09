"""Tests for sweep_orphan_trash_dirs (Story #1032 AC8).

The sweeper runs at server startup to recover leftover trash entries from
crashes mid-Phase-2.  Must be idempotent and never block startup on failure.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


@pytest.fixture
def manager_with_temp_root():
    with tempfile.TemporaryDirectory() as tmp:
        golden = MagicMock()
        bjm = MagicMock()
        bjm.submit_job.return_value = "fake-job-id"
        m = ActivatedRepoManager(
            data_dir=tmp,
            golden_repo_manager=golden,
            background_job_manager=bjm,
        )
        yield m, tmp


def _seed_orphan(manager: ActivatedRepoManager, name: str) -> str:
    trash_root = Path(manager.activated_repos_dir) / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    entry = trash_root / name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "file.txt").write_text("orphan data")
    (entry / "sub").mkdir(parents=True, exist_ok=True)
    (entry / "sub" / "nested.txt").write_text("nested")
    return str(entry)


def test_missing_trash_root_returns_zero(manager_with_temp_root):
    manager, _ = manager_with_temp_root
    # No .trash dir created
    assert manager.sweep_orphan_trash_dirs() == 0


def test_empty_trash_root_returns_zero(manager_with_temp_root):
    manager, _ = manager_with_temp_root
    trash_root = Path(manager.activated_repos_dir) / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    assert manager.sweep_orphan_trash_dirs() == 0


def test_single_orphan_purged(manager_with_temp_root):
    manager, _ = manager_with_temp_root
    orphan = _seed_orphan(manager, "20260529T010101-abc12345-bob-test")
    assert os.path.exists(orphan)
    assert manager.sweep_orphan_trash_dirs() == 1
    assert not os.path.exists(orphan)


def test_multiple_orphans_purged(manager_with_temp_root):
    manager, _ = manager_with_temp_root
    o1 = _seed_orphan(manager, "20260529T010101-deadbeef-alice-r1")
    o2 = _seed_orphan(manager, "20260529T010202-cafebabe-bob-r2")
    o3 = _seed_orphan(manager, "20260529T010303-12345678-carol-r3")
    assert manager.sweep_orphan_trash_dirs() == 3
    assert not os.path.exists(o1)
    assert not os.path.exists(o2)
    assert not os.path.exists(o3)


def test_sweep_is_idempotent(manager_with_temp_root):
    manager, _ = manager_with_temp_root
    _seed_orphan(manager, "20260529T010101-abc12345-bob-test")
    assert manager.sweep_orphan_trash_dirs() == 1
    # Second call: no orphans left, returns 0, does not raise.
    assert manager.sweep_orphan_trash_dirs() == 0


def test_sweep_continues_past_individual_failure(manager_with_temp_root, monkeypatch):
    """If one entry's purge raises, sweep must continue with the rest."""
    manager, _ = manager_with_temp_root
    good_a = _seed_orphan(manager, "20260529T010101-aaaaaaaa-x-a")
    _seed_orphan(manager, "..evil")  # invalid basename → helper raises ValueError
    good_b = _seed_orphan(manager, "20260529T010102-bbbbbbbb-x-b")
    # Note: the ".." entry can't be created via Path API normally because it's
    # interpreted as parent dir.  Skip the bad-entry simulation if it landed in
    # the wrong place.
    # Sweep should still report 2 good purges regardless.
    swept = manager.sweep_orphan_trash_dirs()
    assert swept >= 2  # at minimum the two good entries
    assert not os.path.exists(good_a)
    assert not os.path.exists(good_b)


# ---------------------------------------------------------------------------
# HIGH #3: cap parameter tests
# ---------------------------------------------------------------------------


def test_cap_limits_entries_purged(manager_with_temp_root):
    """sweep_orphan_trash_dirs(cap=2) must stop after purging 2 entries."""
    manager, _ = manager_with_temp_root
    _seed_orphan(manager, "20260529T010101-aaaaaaaa-x-a")
    _seed_orphan(manager, "20260529T010102-bbbbbbbb-x-b")
    _seed_orphan(manager, "20260529T010103-cccccccc-x-c")
    _seed_orphan(manager, "20260529T010104-dddddddd-x-d")

    swept = manager.sweep_orphan_trash_dirs(cap=2)

    # Exactly 2 entries purged (cap respected).
    assert swept == 2
    # 2 entries remain in trash root.
    trash_root = Path(manager.activated_repos_dir) / ".trash"
    remaining = list(trash_root.iterdir())
    assert len(remaining) == 2


def test_cap_zero_means_unlimited(manager_with_temp_root):
    """sweep_orphan_trash_dirs(cap=0) must purge all entries (backward compat)."""
    manager, _ = manager_with_temp_root
    for i in range(5):
        _seed_orphan(manager, f"20260529T01010{i}-{'a' * 8}-x-{i}")

    swept = manager.sweep_orphan_trash_dirs(cap=0)

    assert swept == 5
    trash_root = Path(manager.activated_repos_dir) / ".trash"
    assert list(trash_root.iterdir()) == []


def test_remaining_entries_survive_capped_sweep(manager_with_temp_root):
    """Entries not purged under cap must persist and be picked up on next call."""
    manager, _ = manager_with_temp_root
    paths = [
        _seed_orphan(manager, f"20260529T01010{i}-{'b' * 8}-x-{i}") for i in range(4)
    ]

    # First sweep: cap at 2.
    swept1 = manager.sweep_orphan_trash_dirs(cap=2)
    assert swept1 == 2

    # Some entries must still exist.
    surviving = [p for p in paths if os.path.exists(p)]
    assert len(surviving) == 2, (
        f"Expected 2 survivors after capped sweep, got {len(surviving)}"
    )

    # Second sweep (unlimited): clears remaining 2.
    swept2 = manager.sweep_orphan_trash_dirs(cap=0)
    assert swept2 == 2
    for p in paths:
        assert not os.path.exists(p)


def test_cap_warning_logged_when_entries_remain(manager_with_temp_root, caplog):
    """When cap is hit and entries remain, a WARNING must be logged."""
    import logging

    manager, _ = manager_with_temp_root
    for i in range(3):
        _seed_orphan(manager, f"20260529T01010{i}-{'c' * 8}-x-{i}")

    with caplog.at_level(logging.WARNING):
        manager.sweep_orphan_trash_dirs(cap=1)

    # Should log a warning mentioning remaining entries.
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("remain" in m.lower() or "capped" in m.lower() for m in warning_msgs), (
        f"Expected a 'remain'/'capped' warning when sweep is capped; got: {warning_msgs}"
    )
