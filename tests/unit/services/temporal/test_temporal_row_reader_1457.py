"""read_legacy_shard_rows() -- side-effect-free full row reader for a
temporal shard's on-disk hash-sharded vector_*.json files (Story #1457 AC1
relocation trigger).

Sibling to temporal_row_existence.py's temporal_shard_has_committed_rows()
(same rglob("vector_*.json") scan target), but reads FULL row content
instead of short-circuiting on existence -- this is the reader AC6's
Branch B-bootstrap needs to pass as legacy_row_reader to
execute_temporal_refresh_branch(), and the primitive any future AC11
bootstrap implementation should REUSE rather than reimplement.

Real filesystem, real JSON files written in the EXACT on-disk format
FilesystemVectorStore._prepare_vector_data_batch produces -- no mocking.
"""

from __future__ import annotations

import json

from code_indexer.services.temporal.temporal_row_reader import (
    read_legacy_shard_rows,
)


def _write_row(shard_dir, hash_prefix: str, point_id: str, commit_hash: str):
    shard_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3, 0.4],
        "metadata": {"language": "python", "type": "commit_diff"},
        "payload": {"commit_hash": commit_hash, "path": "src/foo.py"},
    }
    (shard_dir / f"vector_{hash_prefix}.json").write_text(json.dumps(row))


def test_returns_empty_when_shard_dir_missing(tmp_path):
    result = list(read_legacy_shard_rows(tmp_path / "nonexistent"))
    assert result == []


def test_reads_all_rows_from_flat_shard_dir(tmp_path):
    shard_dir = tmp_path / "shard"
    _write_row(shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")
    _write_row(shard_dir, "bbbb2222", "proj:commit:c1:0", "c1")

    rows = list(read_legacy_shard_rows(shard_dir))

    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert ids == {"proj:commit:c0:0", "proj:commit:c1:0"}
    for r in rows:
        assert "vector" in r
        assert "payload" in r


def test_reads_rows_from_hash_sharded_subdirectories(tmp_path):
    """Rows live 4 levels deep in a real hash-sharded layout -- the same
    structure temporal_shard_has_committed_rows()'s rglob already scans."""
    shard_dir = tmp_path / "shard"
    nested = shard_dir / "a" / "a" / "a" / "a"
    _write_row(nested, "aaaa9999", "proj:commit:deep:0", "deep")

    rows = list(read_legacy_shard_rows(shard_dir))

    assert len(rows) == 1
    assert rows[0]["id"] == "proj:commit:deep:0"


def test_skips_malformed_json_file_with_logged_warning(tmp_path, caplog):
    """Matches the established precedent
    FilesystemVectorStore._rebuild_path_index_from_disk already uses for
    this same vector_*.json scan target: skip + log warning, never raise
    (a single corrupt row file must not abort the whole scan)."""
    import logging

    shard_dir = tmp_path / "shard"
    shard_dir.mkdir(parents=True)
    _write_row(shard_dir, "aaaa1111", "proj:commit:good:0", "good")
    (shard_dir / "vector_corrupt.json").write_text("{not valid json")

    with caplog.at_level(logging.WARNING):
        rows = list(read_legacy_shard_rows(shard_dir))

    assert len(rows) == 1
    assert rows[0]["id"] == "proj:commit:good:0"
    assert any("vector_corrupt.json" in record.message for record in caplog.records)


def test_fail_on_corrupt_true_raises_instead_of_skipping(tmp_path):
    """Story #1457 CRITICAL #4 (2026-07-23 code review): the publish path
    (AC6 Branch B-bootstrap / a future AC11 bootstrap) must FAIL LOUD on
    any unreadable/invalid row rather than silently publishing an
    incomplete result -- opt-in via fail_on_corrupt=True, leaving the
    default (every OTHER reuse of this generic scan primitive) unchanged."""
    shard_dir = tmp_path / "shard"
    shard_dir.mkdir(parents=True)
    _write_row(shard_dir, "aaaa1111", "proj:commit:good:0", "good")
    (shard_dir / "vector_corrupt.json").write_text("{not valid json")

    import pytest

    with pytest.raises(RuntimeError, match="vector_corrupt.json"):
        list(read_legacy_shard_rows(shard_dir, fail_on_corrupt=True))
