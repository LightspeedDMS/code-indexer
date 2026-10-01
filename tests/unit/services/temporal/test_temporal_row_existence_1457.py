"""Row-existence-not-queryability primitive (Story #1457 AC6/AC8/AC11).

A temporal quarter/monolith shard directory with real committed rows but no
built `hnsw_index.bin` (e.g. a crash between `upsert_points()` and
`end_indexing()`) MUST still be detected as "has data" -- `hnsw_index.bin`
presence is a QUERYABILITY signal, never a DATA-EXISTENCE signal. This is a
SIDE-EFFECT-FREE, existence-only scan (unlike
`IDIndexManager.rebuild_from_vectors`, which writes `id_index.bin` as a side
effect -- Story #1458 finding F6) that short-circuits on the FIRST committed
row found, rather than enumerating every row file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)


def test_returns_false_for_nonexistent_directory(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert temporal_shard_has_committed_rows(missing) is False


def test_returns_false_for_empty_directory(tmp_path):
    shard_dir = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    shard_dir.mkdir()
    assert temporal_shard_has_committed_rows(shard_dir) is False


def test_returns_true_when_a_row_file_exists_in_a_hash_shard_subdir(tmp_path):
    """Real layout: vector_*.json files are 4-level hash-sharded, not direct
    children of the shard dir."""
    shard_dir = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    nested = shard_dir / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')

    assert temporal_shard_has_committed_rows(shard_dir) is True


def test_directory_with_only_hnsw_index_and_no_rows_returns_false(tmp_path):
    """hnsw_index.bin presence must NEVER substitute for the row-existence
    check -- a directory with an index file but zero vector_*.json rows is
    genuinely empty of data."""
    shard_dir = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    shard_dir.mkdir()
    (shard_dir / "hnsw_index.bin").write_bytes(b"not-real-hnsw-data")

    assert temporal_shard_has_committed_rows(shard_dir) is False


def test_scan_short_circuits_on_first_match(tmp_path, monkeypatch):
    """Proves early-exit, not merely correctness: two row files exist, but
    the scan must consume only ONE item from the directory-walk generator
    before returning True."""
    shard_dir = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    nested = shard_dir / "a"
    nested.mkdir(parents=True)
    (nested / "vector_001.json").write_text("{}")
    (nested / "vector_002.json").write_text("{}")

    consumed = []
    original_rglob = Path.rglob

    def spy_rglob(self, pattern):
        for item in original_rglob(self, pattern):
            consumed.append(item)
            yield item

    monkeypatch.setattr(Path, "rglob", spy_rglob)

    result = temporal_shard_has_committed_rows(shard_dir)

    assert result is True
    assert len(consumed) == 1, (
        "scan must short-circuit after the FIRST match, consumed "
        f"{len(consumed)} items from the directory walk"
    )


def _snapshot(shard_dir: Path) -> dict:
    """Full relative-path + content-hash snapshot of every file under
    shard_dir, strong enough to detect a rewrite-in-place, not just a new
    file appearing."""
    snapshot = {}
    for p in sorted(shard_dir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(shard_dir))
            snapshot[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snapshot


def test_scan_does_not_write_or_modify_any_files(tmp_path):
    """Side-effect-free: unlike IDIndexManager.rebuild_from_vectors, this
    scan must never create id_index.bin, rewrite an existing file, or
    otherwise mutate the shard directory in any way."""
    shard_dir = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    nested = shard_dir / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')

    before = _snapshot(shard_dir)
    temporal_shard_has_committed_rows(shard_dir)
    after = _snapshot(shard_dir)

    assert before == after
