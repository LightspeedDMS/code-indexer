"""Resume, recorded failures and self-heal ask the PASS whether a file exists.

Each of these filtered its candidates with a local ``Path.exists()``. On a pod
without the checkout that is False for every file, and none of them failed --
each quietly did nothing, then cleared the record of what it had been asked
to do:

* a resumed run kept none of its remaining files and completed as done;
* the files the previous run could not index were dropped AND the stored
  retry list was rewritten empty, so they were never retried;
* self-heal-wiped files were skipped, and the pending set that would have
  brought them back is cleared once the run succeeds.

Pinned against that shape: an empty directory here, and a delegated manifest
listing the repository held elsewhere. ``_exists_in_this_pass`` answers from
the manifest when a pass has one, and from the disk exactly as before when not.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.indexing.processor import ProcessingStats
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import ContentEntry
from code_indexer.services.smart_indexer import SmartIndexer

PRESENT = "src/a.py"
GONE = "src/gone.py"
COLLECTION = "voyage-code-3"


@pytest.fixture
def codebase(tmp_path: Path) -> Path:
    """What a mount-less pod holds at the golden repo's path."""
    here = tmp_path / "golden-repos" / "agent-router"
    (here / ".code-indexer").mkdir(parents=True)
    return here


def _manifest(root: Path) -> RepoManifest:
    entry = ContentEntry(path=PRESENT, size=1, blob_sha="a" * 40, is_symlink=False)
    return RepoManifest(root, {PRESENT: entry}, backend_is_local=False)


def _indexer(codebase: Path, manifest: Optional[RepoManifest]) -> Any:
    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.config = SimpleNamespace(
        codebase_dir=codebase, confined_to_codebase_root=True
    )
    indexer.repo_manifest = manifest
    indexer.vector_store_client = MagicMock()
    indexer.vector_store_client.get_pending_self_heal_reprocess_paths.return_value = (
        frozenset({PRESENT, GONE})
    )
    indexer.vector_store_client.resolve_collection_name.return_value = COLLECTION
    indexer.progressive_metadata = MagicMock()
    indexer.file_finder = MagicMock()
    indexer.file_finder.is_eligible.return_value = True
    indexer.progress_log = MagicMock()
    indexer.embedding_provider = MagicMock()
    processed: List[List[Path]] = []

    def process(files: List[Path], **kw: Any) -> ProcessingStats:
        processed.append(list(files))
        return ProcessingStats()

    indexer.process_files_high_throughput = process
    indexer.processed = processed
    return indexer


def test_pending_self_heal_paths_are_folded_in_from_the_manifest(
    codebase: Path,
) -> None:
    indexer = _indexer(codebase, _manifest(codebase))
    _, merged = indexer._fold_in_pending_self_heal_paths(COLLECTION, [])
    assert merged == [codebase / PRESENT]


def test_newly_pending_self_heal_paths_are_reprocessed(codebase: Path) -> None:
    indexer = _indexer(codebase, _manifest(codebase))
    indexer._reprocess_newly_pending_self_heal_paths(
        COLLECTION, [], ProcessingStats(), 1, None, None
    )
    assert indexer.processed == [[codebase / PRESENT]]


def test_recorded_failures_are_retried_and_not_wiped(codebase: Path) -> None:
    indexer = _indexer(codebase, _manifest(codebase))
    indexer.progressive_metadata.metadata = {
        "failed_file_paths": [str(codebase / PRESENT), str(codebase / GONE)]
    }

    retried, merged = indexer._merge_recorded_failures([])

    assert retried == [codebase / PRESENT]
    assert merged == [codebase / PRESENT]
    indexer.progressive_metadata.set_failed_file_paths.assert_called_once_with(
        [str(codebase / PRESENT)]
    )


def test_a_resumed_run_processes_its_remaining_files(codebase: Path) -> None:
    indexer = _indexer(codebase, _manifest(codebase))
    indexer.vector_store_client.get_pending_self_heal_reprocess_paths.return_value = (
        frozenset()
    )
    indexer.progressive_metadata.metadata = {}
    indexer.progressive_metadata.get_remaining_files.return_value = [
        str(codebase / PRESENT),
        str(codebase / GONE),
    ]
    indexer._finalize_or_abort_indexing_session = MagicMock()

    indexer._do_resume_interrupted(
        50, None, {}, "voyage-ai", "voyage-code-3", vector_thread_count=1
    )

    assert indexer.processed == [[codebase / PRESENT]]


def test_without_a_manifest_the_disk_still_decides(codebase: Path) -> None:
    """The local path is unchanged: only files on this disk count."""
    (codebase / "src").mkdir()
    (codebase / GONE).write_text("x\n")
    indexer = _indexer(codebase, None)
    _, merged = indexer._fold_in_pending_self_heal_paths(COLLECTION, [])
    assert merged == [codebase / GONE]
