"""Unit test for Bug #1969 Round 6, finding P1-3 (BLOCKING): the sidecar
is cleared after FAILED reprocessing. `_clear_self_heal_reprocess_paths_
if_safe` in `smart_indexer.py` only checks `stats.cancelled`, not
`stats.failed_files`. If a provider or chunking failure hits a pending
file, `failed_files > 0`, `cancelled=False`, and its only replay record
is deleted -- exactly like every OTHER consulted path, even ones that
genuinely never got their points back.

Fix: clear only the paths that were actually reprocessed successfully
(verified by checking they now have real points again), keeping the
rest durably recorded so one persistently failing file can't pin every
other entry.

The checks use a real `SmartIndexer`/`FilesystemVectorStore` pair and a
real durable sidecar. The retry check stubs only the processing pipeline
to return a deterministic per-run failure.
"""

import json
import logging
from pathlib import Path

from code_indexer.indexing.processor import ProcessingStats
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.storage.shared.collection_dedup_repair import (
    read_pending_self_heal_reprocess_paths,
    record_self_heal_reprocess_pending,
)
from tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess import (
    _init_repo,
    _make_smart_indexer,
    _run_git,
)


def test_clear_keeps_failed_path_but_clears_successful_one(
    tmp_path: Path, caplog
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "file_a.py").write_text("# file a -- successfully reprocessed\n")
    (repo / "file_b.py").write_text("# file b -- reprocess attempt fails\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")

    metadata_path = tmp_path / "metadata.json"
    indexer = _make_smart_indexer(repo, metadata_path)
    indexer.smart_index()

    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name

    # Simulate file_b.py's reprocess attempt having genuinely failed: it
    # ends this run with ZERO points (as a real provider/chunking failure
    # would leave it), while file_a.py's points remain intact (as if IT
    # was successfully reprocessed).
    for f in list(collection_path.rglob("vector_*.json")):
        record = json.loads(f.read_text())
        if record.get("payload", {}).get("path") == "file_b.py":
            f.unlink()

    record_self_heal_reprocess_pending(
        collection_path, frozenset({"file_a.py", "file_b.py"})
    )

    stats = ProcessingStats()
    stats.failed_files = 1
    stats.failed_paths = frozenset({"file_b.py"})
    stats.cancelled = False

    indexer._clear_self_heal_reprocess_paths_if_safe(
        collection_name, frozenset({"file_a.py", "file_b.py"}), stats
    )

    remaining = read_pending_self_heal_reprocess_paths(collection_path)
    assert remaining == frozenset({"file_b.py"}), (
        "file_a.py (which still has real points) must be cleared from "
        "the durable sidecar, but file_b.py (zero points -- its "
        "reprocess attempt genuinely failed) must remain recorded so "
        "its only replay path is not lost, even though "
        f"stats.failed_files > 0 for the run overall. Got: {remaining!r}"
    )

    # A processor can report success while producing zero chunks. The
    # durable replay record must still remain until points really exist.
    zero_chunk_stats = ProcessingStats(files_processed=1, chunks_created=0)
    with caplog.at_level(logging.WARNING):
        indexer._clear_self_heal_reprocess_paths_if_safe(
            collection_name, frozenset({"file_b.py"}), zero_chunk_stats
        )
    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset(
        {"file_b.py"}
    )
    assert any(
        record.levelno >= logging.WARNING and "file_b.py" in record.getMessage()
        for record in caplog.records
    )


def test_failed_pending_path_is_attempted_once_per_run_and_warned(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    failed_file = repo / "failed.py"
    failed_file.write_text("# provider failure leaves this file without points\n")
    indexer = _make_smart_indexer(repo, tmp_path / "metadata.json")
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    record_self_heal_reprocess_pending(collection_path, frozenset({"failed.py"}))

    attempted_files = []

    def fail_processing(*, files, **_kwargs):
        attempted_files.extend(files)
        return ProcessingStats(failed_files=len(files))

    monkeypatch.setattr(indexer, "process_files_high_throughput", fail_processing)

    with caplog.at_level(logging.WARNING):
        for run_number in (1, 2):
            pending, selected = indexer._fold_in_pending_self_heal_paths(
                collection_name, []
            )
            assert pending == frozenset({"failed.py"})
            assert selected == [failed_file]

            stats = indexer.process_files_high_throughput(files=selected)  # type: ignore[call-arg]  # monkeypatched to `fail_processing` above
            stats, newly_pending = indexer._reprocess_newly_pending_self_heal_paths(
                collection_name, selected, stats, 1, None, None
            )
            indexer._clear_self_heal_reprocess_paths_if_safe(
                collection_name, pending | newly_pending, stats
            )

            assert attempted_files == [failed_file] * run_number, (
                "a failed pending path gets one attempt in each run, with "
                "no same-run retry loop"
            )
            assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset(
                {"failed.py"}
            )

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "failed.py" in record.getMessage()
    ]
    assert len(warnings) == 2, "each failed run must warn about its retained path"


def test_failed_reprocess_does_not_clear_preexisting_points(
    tmp_path: Path, caplog
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "stale.py").write_text("def old_content():\n    return 1\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")
    indexer = _make_smart_indexer(repo, tmp_path / "metadata.json")
    indexer.smart_index()
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name

    # Recovery records the sidecar BEFORE deletion. A crash at that point
    # can leave old points in place; their presence does not prove that a
    # later, failed reprocess succeeded.
    assert any(
        json.loads(path.read_text()).get("payload", {}).get("path") == "stale.py"
        for path in collection_path.rglob("vector_*.json")
    ), "setup must retain an old point for the failed pending path"
    record_self_heal_reprocess_pending(collection_path, frozenset({"stale.py"}))
    failed_stats = ProcessingStats(failed_files=1)
    with caplog.at_level(logging.WARNING):
        indexer._clear_self_heal_reprocess_paths_if_safe(
            collection_name, frozenset({"stale.py"}), failed_stats
        )

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset(
        {"stale.py"}
    )
    assert any(
        record.levelno >= logging.WARNING and "stale.py" in record.getMessage()
        for record in caplog.records
    )


def test_unrelated_zero_point_path_does_not_explain_failed_stale_path(
    tmp_path: Path,
) -> None:
    """An aggregate failure count cannot identify which pending path failed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "stale.py").write_text("def old_content():\n    return 1\n")
    (repo / "empty.py").write_text("def old_content():\n    return 2\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")
    indexer = _make_smart_indexer(repo, tmp_path / "metadata.json")
    indexer.smart_index()
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name

    # Recovery records both paths before deletion. The stale path's
    # reprocessing fails while its old point survives; the other path
    # successfully produces zero chunks and therefore has no points.
    for point_file in collection_path.rglob("vector_*.json"):
        point = json.loads(point_file.read_text())
        if point.get("payload", {}).get("path") == "empty.py":
            point_file.unlink()
    assert any(
        json.loads(path.read_text()).get("payload", {}).get("path") == "stale.py"
        for path in collection_path.rglob("vector_*.json")
    )
    record_self_heal_reprocess_pending(
        collection_path, frozenset({"stale.py", "empty.py"})
    )

    indexer._clear_self_heal_reprocess_paths_if_safe(
        collection_name,
        frozenset({"stale.py", "empty.py"}),
        ProcessingStats(failed_files=1, files_processed=1, chunks_created=0),
    )

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset(
        {"stale.py", "empty.py"}
    ), "neither path is proven successfully reprocessed; retain both records"


def test_submission_failure_keeps_pending_path_with_preexisting_points(
    tmp_path: Path, monkeypatch
) -> None:
    """A failure before a file future exists must retain its replay marker.

    Recovery writes the marker before deleting old points, so those points
    can survive a crash. A later submission failure must be attributed to
    that path rather than mistaken for a successful reprocess.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source = repo / "stale.py"
    source.write_text("def old_content():\n    return 1\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")
    metadata_path = tmp_path / "metadata.json"
    initial_indexer = _make_smart_indexer(repo, metadata_path)
    initial_indexer.smart_index()

    indexer = _make_smart_indexer(repo, metadata_path)
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    assert any(
        json.loads(path.read_text()).get("payload", {}).get("path") == "stale.py"
        for path in collection_path.rglob("vector_*.json")
    ), "setup must retain an old point whose presence cannot prove reprocessing"
    record_self_heal_reprocess_pending(collection_path, frozenset({"stale.py"}))

    def reject_submission(*_args, **_kwargs):
        raise RuntimeError("synthetic submission failure")

    monkeypatch.setattr(
        FileChunkingManager, "submit_file_for_processing", reject_submission
    )
    stats = indexer.process_files_high_throughput(files=[source], vector_thread_count=1)
    indexer._clear_self_heal_reprocess_paths_if_safe(
        collection_name, frozenset({"stale.py"}), stats
    )

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset(
        {"stale.py"}
    ), "a rejected submission cannot clear its only durable replay path"
    assert stats.failed_files == 1
    assert stats.failed_paths == frozenset({"stale.py"})


def test_second_pass_merges_failed_path_attribution_before_clearing(
    tmp_path: Path, monkeypatch
) -> None:
    """A second-pass failure must not pin another path that succeeded."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    succeeded = repo / "succeeded.py"
    failed = repo / "failed.py"
    succeeded.write_text("def succeeded():\n    return 1\n")
    failed.write_text("def failed():\n    return 2\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")
    metadata_path = tmp_path / "metadata.json"
    initial_indexer = _make_smart_indexer(repo, metadata_path)
    initial_indexer.smart_index()

    indexer = _make_smart_indexer(repo, metadata_path)
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    indexed_paths = {
        json.loads(path.read_text()).get("payload", {}).get("path")
        for path in collection_path.rglob("vector_*.json")
    }
    assert {"succeeded.py", "failed.py"} <= indexed_paths
    record_self_heal_reprocess_pending(
        collection_path, frozenset({"succeeded.py", "failed.py"})
    )

    reprocess_stats = ProcessingStats(files_processed=1, failed_files=1)
    # Assignment also works on committed HEAD, whose ProcessingStats does
    # not yet declare this field, so its RED remains behavioral.
    reprocess_stats.failed_paths = frozenset({"failed.py"})
    selected_files = []

    def process_second_pass(*, files, **_kwargs):
        selected_files.extend(files)
        return reprocess_stats

    monkeypatch.setattr(indexer, "process_files_high_throughput", process_second_pass)
    stats, pending = indexer._reprocess_newly_pending_self_heal_paths(
        collection_name, [], ProcessingStats(), 1, None, None
    )
    assert set(selected_files) == {succeeded, failed}
    indexer._clear_self_heal_reprocess_paths_if_safe(collection_name, pending, stats)

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset(
        {"failed.py"}
    ), "the second pass must clear its successful path and retain its failed path"
    assert stats.failed_paths == frozenset({"failed.py"})
