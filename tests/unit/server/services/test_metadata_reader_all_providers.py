"""metadata_reader helpers that read every provider's index metadata and
tell whether an indexing run changed the index (real files, no mocks)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.server.services.metadata_reader import (
    IndexState,
    index_unchanged_since,
    read_index_states,
    snapshot_index_metadata,
)


def _write(repo: Path, name: str, payload: object) -> None:
    meta_dir = repo / ".code-indexer"
    meta_dir.mkdir(exist_ok=True)
    (meta_dir / name).write_text(json.dumps(payload))


def test_reads_every_provider_in_name_order_and_ignores_legacy(tmp_path: Path) -> None:
    _write(tmp_path, "metadata.json", {"status": "failed"})
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"status": "completed", "current_commit": "a" * 40},
    )
    _write(tmp_path, "metadata-cohere.json", {"status": "in_progress"})

    assert read_index_states(tmp_path) == [
        IndexState("metadata-cohere.json", "in_progress", None),
        IndexState("metadata-voyage-ai.json", "completed", "a" * 40),
    ]


def test_legacy_file_read_only_without_provider_files(tmp_path: Path) -> None:
    assert read_index_states(tmp_path) == []
    _write(tmp_path, "metadata.json", {"status": "failed"})
    assert read_index_states(str(tmp_path)) == [
        IndexState("metadata.json", "failed", None)
    ]


def test_non_path_argument_rejected() -> None:
    with pytest.raises(TypeError):
        read_index_states(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "after, unchanged",
    [
        ({"run_sequence": 2, "last_run_changed_index": False}, True),
        ({"run_sequence": 2, "last_run_changed_index": True}, False),
        ({"run_sequence": 2}, False),
        (["not", "an", "object"], False),
    ],
)
def test_rewritten_file_counts_by_its_flag(
    tmp_path: Path, after: object, unchanged: bool
) -> None:
    _write(tmp_path, "metadata-cohere.json", {"run_sequence": 1})
    before = snapshot_index_metadata(tmp_path)
    _write(tmp_path, "metadata-cohere.json", after)
    assert index_unchanged_since(tmp_path, before) is unchanged


def test_identical_file_is_no_run(tmp_path: Path) -> None:
    _write(tmp_path, "metadata-cohere.json", {"last_run_changed_index": True})
    before = snapshot_index_metadata(tmp_path)
    assert index_unchanged_since(tmp_path, before) is True


def test_malformed_rewritten_file_counts_as_changed(tmp_path: Path) -> None:
    _write(tmp_path, "metadata-cohere.json", {"run_sequence": 1})
    before = snapshot_index_metadata(tmp_path)
    (tmp_path / ".code-indexer" / "metadata-cohere.json").write_text("{not json")
    assert index_unchanged_since(tmp_path, before) is False


def test_new_provider_file_counts_by_its_flag(tmp_path: Path) -> None:
    _write(tmp_path, "metadata-cohere.json", {"run_sequence": 1})
    before = snapshot_index_metadata(tmp_path)
    _write(tmp_path, "metadata-voyage-ai.json", {"last_run_changed_index": True})
    assert index_unchanged_since(tmp_path, before) is False
