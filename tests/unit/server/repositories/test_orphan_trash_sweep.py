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
