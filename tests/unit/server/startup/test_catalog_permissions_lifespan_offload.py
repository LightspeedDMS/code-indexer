"""Catalog permission work started by the server lifespan runs on a worker
thread, never on the event loop."""

import logging
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import anyio.to_thread
import pytest

from code_indexer.server.services import memory_store_service_factory
from code_indexer.server.startup import lifespan
from code_indexer.server.storage.shared import catalog_tree_permissions
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _retained_catalog_snapshot(base: Path) -> Path:
    memories = base / ".versioned" / "cidx-meta" / "v_1700000001" / "memories"
    memories.mkdir(parents=True)
    memory = memories / "mem-0001.md"
    memory.write_text("note")
    os.chmod(memory, 0o644)
    return memory


async def test_catalog_snapshot_repair_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "versioned_base"
    memory = _retained_catalog_snapshot(base)
    manager = VersionedSnapshotManager(
        clone_backend=LocalCloneBackend(versioned_base=str(base)),
        versioned_base=str(base),
    )
    threads: List[int] = []
    real_repair = catalog_tree_permissions.repair_retained_catalog_snapshots

    def spy(snapshot_manager: Any) -> int:
        threads.append(threading.get_ident())
        return real_repair(snapshot_manager)

    monkeypatch.setattr(
        catalog_tree_permissions, "repair_retained_catalog_snapshots", spy
    )

    await lifespan._start_catalog_snapshot_mode_repair(manager)

    assert _mode(memory) == 0o640
    assert threads and threads[0] != threading.get_ident()


async def test_catalog_snapshot_repair_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.list_snapshots.side_effect = OSError("storage unavailable")

    with caplog.at_level(logging.WARNING):
        await lifespan._start_catalog_snapshot_mode_repair(manager)

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def _app_with(task: Any) -> Any:
    state = SimpleNamespace(catalog_snapshot_mode_repair_task=task)
    return SimpleNamespace(state=state)


async def test_shutdown_does_not_wait_for_a_blocked_repair(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked(snapshot_manager: Any) -> int:
        started.set()
        release.wait(10)
        return 0

    monkeypatch.setattr(
        catalog_tree_permissions, "repair_retained_catalog_snapshots", blocked
    )
    app = _app_with(lifespan._start_catalog_snapshot_mode_repair(MagicMock()))
    await anyio.to_thread.run_sync(started.wait, 5)

    began = time.monotonic()
    try:
        with caplog.at_level(logging.INFO):
            await lifespan._stop_catalog_snapshot_mode_repair(app)
        elapsed = time.monotonic() - began
    finally:
        release.set()

    assert elapsed < 2
    assert app.state.catalog_snapshot_mode_repair_task.done()
    assert any("still running" in r.getMessage() for r in caplog.records)


async def test_shutdown_without_repair_task_is_quiet() -> None:
    await lifespan._stop_catalog_snapshot_mode_repair(_app_with(None))


async def test_shutdown_after_repair_finished_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.list_snapshots.return_value = []
    task = lifespan._start_catalog_snapshot_mode_repair(manager)
    await task

    with caplog.at_level(logging.INFO):
        await lifespan._stop_catalog_snapshot_mode_repair(_app_with(task))

    assert not any("still running" in r.getMessage() for r in caplog.records)


async def test_memory_store_bundle_is_built_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    (golden_repos_dir / "cidx-meta").mkdir(parents=True)
    threads: List[int] = []
    real_build = memory_store_service_factory.build_memory_store_service

    def spy(**kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return real_build(**kwargs)

    monkeypatch.setattr(memory_store_service_factory, "build_memory_store_service", spy)

    bundle = await lifespan._build_memory_store_bundle(
        golden_repos_dir=golden_repos_dir,
        server_data_dir=tmp_path / "data",
        refresh_scheduler=MagicMock(),
        refresh_debouncer=MagicMock(),
    )

    assert bundle.memories_dir.is_dir()
    assert _mode(bundle.memories_dir) & stat.S_IRWXO == 0
    assert threads and threads[0] != threading.get_ident()
