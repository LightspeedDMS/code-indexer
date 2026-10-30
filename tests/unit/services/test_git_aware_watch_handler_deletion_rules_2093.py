"""#2093: a deletion event is queued only when the full walk would have
indexed the path (FileFinder.matches_exclude_pattern plus the override
filter), so the store's own files are never treated as deleted sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, cast

import pytest
from watchdog.events import FileDeletedEvent

from code_indexer.config import Config, OverrideConfig
from code_indexer.services.git_aware_watch_handler import GitAwareWatchHandler
from code_indexer.services.git_topology_service import GitTopologyService
from code_indexer.services.watch_metadata import WatchMetadata


def _handler(
    root: Path, override: Optional[OverrideConfig] = None
) -> GitAwareWatchHandler:
    config = Config(codebase_dir=root)
    if override is not None:
        config.override_config = override
    return GitAwareWatchHandler(
        config=config,
        # ^ why cast, not a real SmartIndexer; queueing an event never touches the indexer
        smart_indexer=cast(Any, None),
        git_topology_service=GitTopologyService(root, config),
        watch_metadata=WatchMetadata(),
        debounce_seconds=0.1,
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".code-indexer/index/voyage-code-3/vector_probe.json",
        ".code-indexer/metadata-voyage-ai.json",
        "node_modules/pkg/index.js",
    ],
)
def test_deleted_file_in_excluded_directory_is_not_queued(
    tmp_path: Path, relative: str
) -> None:
    handler = _handler(tmp_path)

    handler.on_deleted(FileDeletedEvent(str(tmp_path / relative)))

    assert handler.pending_changes == set()


def test_deleted_gitignored_file_is_not_queued(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("generated/\n")
    handler = _handler(tmp_path)

    handler.on_deleted(FileDeletedEvent(str(tmp_path / "generated" / "out.py")))

    assert handler.pending_changes == set()


def test_deleted_file_excluded_by_override_is_not_queued(tmp_path: Path) -> None:
    override = OverrideConfig(
        add_extensions=[],
        remove_extensions=[],
        add_exclude_dirs=[],
        add_include_dirs=[],
        force_include_patterns=[],
        force_exclude_patterns=["src/secret_*.py"],
    )
    handler = _handler(tmp_path, override)

    handler.on_deleted(FileDeletedEvent(str(tmp_path / "src" / "secret_one.py")))

    assert handler.pending_changes == set()


def test_deleted_included_source_file_is_queued(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    deleted = tmp_path / "src" / "module.py"

    handler.on_deleted(FileDeletedEvent(str(deleted)))

    assert handler.pending_changes == {deleted}
