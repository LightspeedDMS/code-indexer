from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from watchdog.events import FileDeletedEvent

from code_indexer.config import Config
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.watch_metadata import WatchMetadata


def _handler(root: Path) -> GitAwareWatchHandler:
    config = Config(codebase_dir=root)
    return GitAwareWatchHandler(
        config=config,
        smart_indexer=cast(Any, None),
        git_topology_service=GitTopologyService(root, config),
        watch_metadata=WatchMetadata(),
        debounce_seconds=0.1,
    )


@pytest.mark.parametrize(
    "relative_name",
    [b"src/bad\xff.py", b".snapshot-reader-leases/abc-lease.py"],
)
def test_deleted_never_indexed_file_is_not_queued(
    tmp_path: Path, relative_name: bytes
) -> None:
    handler = _handler(tmp_path)
    deleted = os.fsdecode(os.path.join(os.fsencode(tmp_path), relative_name))

    handler.on_deleted(FileDeletedEvent(deleted))

    assert handler.pending_changes == set()


def test_deleted_indexable_file_is_queued(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    deleted = tmp_path / "src" / "ok.py"

    handler.on_deleted(FileDeletedEvent(str(deleted)))

    assert handler.pending_changes == {deleted}
