"""issue-64616: chunk coverage of the text index.

Files indexed while image chunks bypassed the text index are missing some of
their chunks. ``incomplete_chunk_paths`` finds them from the stored payloads
alone; the version helpers say whether an index was already checked; the
refresh scheduler's CHUNK_COVERAGE signal forces the one check an idle
repository would otherwise never get.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos.stale_index_signal import (
    CHUNK_COVERAGE_SIGNAL,
    COMMIT_DRIFT_SIGNAL,
    OWED_FAILURES_SIGNAL,
    StaleSignal,
    chunk_coverage_signal,
)
from code_indexer.server.services.metadata_reader import (
    IndexState,
    read_index_states,
)
from code_indexer.services.chunk_coverage import (
    CHUNK_COVERAGE_KEY,
    CHUNK_COVERAGE_VERSION,
    coverage_is_current,
    incomplete_chunk_paths,
    recorded_coverage_version,
)
from code_indexer.services.progressive_metadata import ProgressiveMetadata


def _point(
    path: str,
    index: Any,
    total: Any,
    file_hash: str = "sha256:a",
    hidden: Optional[List[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "path": path,
        "chunk_index": index,
        "total_chunks": total,
        "file_hash": file_hash,
    }
    if hidden is not None:
        payload["hidden_branches"] = hidden
    return {"id": f"{path}-{index}", "payload": payload}


# ── incomplete_chunk_paths ──────────────────────────────────────────────────


def test_a_file_missing_a_chunk_is_incomplete() -> None:
    points = [_point("doc.htm", 0, 3), _point("doc.htm", 2, 3)]
    assert incomplete_chunk_paths(points, "master") == {"doc.htm"}


def test_a_file_with_every_chunk_is_complete() -> None:
    points = [_point("a.py", i, 3) for i in range(3)] + [_point("b.py", 0, 1)]
    assert incomplete_chunk_paths(points, "master") == set()


def test_a_file_whose_every_chunk_was_dropped_is_not_in_the_store_at_all() -> None:
    """Such a file has no point to judge: reconcile already sees it missing."""
    assert incomplete_chunk_paths([_point("a.py", 0, 1)], None) == set()


def test_another_branchs_version_is_not_judged() -> None:
    """An old version hidden on this branch stays as it is; only the visible
    version decides, so re-processing the file clears the path."""
    points = [
        _point("doc.htm", 0, 2, file_hash="sha256:old", hidden=["master"]),
        _point("doc.htm", 0, 2, file_hash="sha256:new"),
        _point("doc.htm", 1, 2, file_hash="sha256:new"),
    ]
    assert incomplete_chunk_paths(points, "master") == set()
    assert incomplete_chunk_paths(points, None) == {"doc.htm"}


def test_versions_are_judged_separately() -> None:
    points = [
        _point("doc.htm", 0, 2, file_hash="sha256:one"),
        _point("doc.htm", 1, 2, file_hash="sha256:two"),
    ]
    assert incomplete_chunk_paths(points, None) == {"doc.htm"}


@pytest.mark.parametrize(
    "index, total",
    [(None, 2), (0, None), ("0", 2), (True, 2), (-1, 2), (0, 1.5)],
)
def test_points_without_usable_chunk_numbers_are_not_judged(
    index: Any, total: Any
) -> None:
    assert incomplete_chunk_paths([_point("legacy.py", index, total)], None) == set()


def test_points_without_a_path_or_payload_are_skipped() -> None:
    points: List[Dict[str, Any]] = [{"id": "x"}, {"id": "y", "payload": {}}]
    assert incomplete_chunk_paths(points, None) == set()


# ── the recorded version ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "metadata, version",
    [
        ({}, 0),
        ({CHUNK_COVERAGE_KEY: "1"}, 0),
        ({CHUNK_COVERAGE_KEY: True}, 0),
        ({CHUNK_COVERAGE_KEY: CHUNK_COVERAGE_VERSION}, CHUNK_COVERAGE_VERSION),
    ],
)
def test_recorded_version(metadata: Dict[str, Any], version: int) -> None:
    assert recorded_coverage_version(metadata) == version
    assert coverage_is_current(metadata) is (version >= CHUNK_COVERAGE_VERSION)


def test_progressive_metadata_records_the_current_version(tmp_path: Path) -> None:
    path = tmp_path / "metadata-voyage-ai.json"
    metadata = ProgressiveMetadata(path)
    assert not coverage_is_current(metadata.metadata)

    metadata.mark_chunk_coverage_current()

    assert json.loads(path.read_text())[CHUNK_COVERAGE_KEY] == CHUNK_COVERAGE_VERSION
    assert coverage_is_current(ProgressiveMetadata(path).metadata)


def test_multimodal_gaps_are_counted_per_run(tmp_path: Path) -> None:
    metadata = ProgressiveMetadata(tmp_path / "metadata-voyage-ai.json")
    metadata.start_indexing("voyage-ai", "voyage-code-3", {})
    metadata.update_progress(files_processed=1, multimodal_chunks_failed=2)
    metadata.update_progress(files_processed=1, multimodal_chunks_failed=1)
    assert metadata.metadata["multimodal_chunks_failed"] == 3

    metadata.start_indexing("voyage-ai", "voyage-code-3", {})

    assert metadata.metadata["multimodal_chunks_failed"] == 0


# ── the refresh scheduler's signal ──────────────────────────────────────────


def _state(status: Optional[str], version: int = 0, source: str = "m.json") -> Any:
    return IndexState(
        source=source,
        status=status,
        current_commit="abc1234",
        chunk_coverage_version=version,
    )


def test_an_unchecked_completed_index_is_signalled() -> None:
    signal = chunk_coverage_signal([_state("completed")], "HEAD1", "repo-global")
    assert signal is not None
    assert signal.kind == CHUNK_COVERAGE_SIGNAL
    assert "chunk_coverage_version=0" in signal.key and "HEAD1" in signal.key
    assert signal.all_completed is True
    assert signal.needs_reconcile is False
    assert signal.may_skip_unchanged_publish is True


def test_a_checked_index_is_not_signalled() -> None:
    states = [_state("completed", CHUNK_COVERAGE_VERSION)]
    assert chunk_coverage_signal(states, "HEAD1", "repo-global") is None


@pytest.mark.parametrize("status", ["in_progress", "failed", None])
def test_an_incomplete_run_is_the_status_signals_concern(
    status: Optional[str],
) -> None:
    assert chunk_coverage_signal([_state(status)], "HEAD1", "repo-global") is None


def test_every_provider_is_checked() -> None:
    states = [
        _state("completed", CHUNK_COVERAGE_VERSION, "metadata-cohere.json"),
        _state("completed", 0, "metadata-voyage-ai.json"),
    ]
    signal = chunk_coverage_signal(states, "HEAD1", "repo-global")
    assert signal is not None and "metadata-voyage-ai.json" in signal.key


@pytest.mark.parametrize("kind", [COMMIT_DRIFT_SIGNAL, OWED_FAILURES_SIGNAL])
def test_other_signals_still_force_a_reconcile(kind: str) -> None:
    assert StaleSignal("k", "m", kind, True).needs_reconcile is True


def test_the_reader_carries_the_recorded_version(tmp_path: Path) -> None:
    meta_dir = tmp_path / ".code-indexer"
    meta_dir.mkdir()
    (meta_dir / "metadata-voyage-ai.json").write_text(
        json.dumps({"status": "completed", CHUNK_COVERAGE_KEY: 1})
    )
    (meta_dir / "metadata-cohere.json").write_text(json.dumps({"status": "completed"}))

    versions = {s.source: s.chunk_coverage_version for s in read_index_states(tmp_path)}

    assert versions == {"metadata-cohere.json": 0, "metadata-voyage-ai.json": 1}
