"""AC5 fix: `_temporal_vectors_exist` layout-agnostic row-existence check
(Story #1457).

The confirmed bug: golden_repo_manager.py's inline
`_temporal_vectors_exist = any(... for _coll_dir.glob("vector_*.json") ...)`
runs a NON-RECURSIVE glob against each `code-indexer-temporal*` collection
directory, but vector_*.json files are actually 4-level hash-sharded --
so this can NEVER match, making `_temporal_vectors_exist` always False and
forcing `--clear` (a full unwanted re-embed) on every explicit
add_indexes/reindex admin action.

The fix reuses the shared `temporal_shard_has_committed_rows` primitive
(Story #1457 AC6/AC8/AC11's row-existence-not-queryability scan), which
recursively scans and correctly detects real hash-sharded rows.
"""

from __future__ import annotations

from code_indexer.server.repositories.golden_repo_manager import (
    _temporal_vectors_exist_for_repo,
)


def test_returns_false_when_no_temporal_directories_exist(tmp_path):
    index_dir = tmp_path / ".code-indexer" / "index"
    index_dir.mkdir(parents=True)
    assert _temporal_vectors_exist_for_repo(index_dir) is False


def test_returns_true_for_hash_sharded_rows_the_old_shallow_glob_would_miss(tmp_path):
    """The exact confirmed bug scenario: real committed rows exist, but only
    inside the 4-level hash-sharded subdirectory structure -- a
    non-recursive glob against the collection dir directly finds nothing."""
    index_dir = tmp_path / ".code-indexer" / "index"
    coll_dir = index_dir / "code-indexer-temporal-voyage_code_3-2024Q1"
    nested = coll_dir / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')

    assert _temporal_vectors_exist_for_repo(index_dir) is True
