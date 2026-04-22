"""
Tests for MemoryMetadataCache — Story #877 Phase 3-A.

Covers: cache hit, miss, TTL expiry, LRU eviction (with re-access verification)
when over max_entries, invalidate (including idempotent behavior), invalidate_all,
corrupt YAML frontmatter returns None (fail-closed), referenced_repo field
preservation, and thread safety smoke test with verified thread completion.

Real filesystem (tmp_path), real memory_io. No mocks for the cache itself.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.services.memory_io import atomic_write_memory_file
from code_indexer.server.services.memory_metadata_cache import MemoryMetadataCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_memory_file(
    memories_dir: Path,
    memory_id: str,
    scope: str = "global",
    referenced_repo: str | None = None,
) -> Path:
    """Write a minimal valid memory file and return its path."""
    memories_dir.mkdir(parents=True, exist_ok=True)
    fm: Dict[str, Any] = {
        "id": memory_id,
        "type": "architectural-fact",
        "scope": scope,
        "summary": "test summary",
        "tags": [],
        "created_by": "test",
        "created_at": "2024-01-01T00:00:00+00:00",
        "edited_by": None,
        "edited_at": None,
    }
    if referenced_repo is not None:
        fm["referenced_repo"] = referenced_repo
    path = memories_dir / f"{memory_id}.md"
    atomic_write_memory_file(path, fm)
    return path


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def memories_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Cache miss: file does not exist → returns None
# ---------------------------------------------------------------------------

def test_cache_miss_returns_none_for_missing_file(memories_dir: Path) -> None:
    cache = MemoryMetadataCache(memories_dir)
    result = cache.get("nonexistent00000000000000000000000")
    assert result is None


# ---------------------------------------------------------------------------
# Cache hit: file exists → returns frontmatter dict
# ---------------------------------------------------------------------------

def test_cache_hit_returns_frontmatter_dict(memories_dir: Path) -> None:
    memory_id = "aabbccdd" * 4  # 32 hex chars
    _write_memory_file(memories_dir, memory_id, scope="global")

    cache = MemoryMetadataCache(memories_dir)
    result = cache.get(memory_id)

    assert result is not None
    assert result["id"] == memory_id
    assert result["scope"] == "global"


# ---------------------------------------------------------------------------
# Cache hit: second call returns cached data (file removed between calls)
# ---------------------------------------------------------------------------

def test_cache_hit_serves_from_cache_on_second_call(memories_dir: Path) -> None:
    memory_id = "11223344" * 4
    path = _write_memory_file(memories_dir, memory_id, scope="global")

    cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    first = cache.get(memory_id)
    assert first is not None

    # Remove file — second call should still return cached result
    path.unlink()
    second = cache.get(memory_id)
    assert second is not None
    assert second["id"] == memory_id


# ---------------------------------------------------------------------------
# TTL expiry: after TTL, cache re-reads from disk
# ---------------------------------------------------------------------------

def test_ttl_expiry_triggers_reread(memories_dir: Path) -> None:
    memory_id = "deadbeef" * 4
    path = _write_memory_file(memories_dir, memory_id, scope="global")

    fake_time = [0.0]
    cache = MemoryMetadataCache(memories_dir, ttl_seconds=10, _clock=lambda: fake_time[0])

    # First read
    first = cache.get(memory_id)
    assert first is not None
    assert first["summary"] == "test summary"

    # Modify the file while cache holds stale entry
    fm2 = dict(first)
    fm2["summary"] = "updated"
    atomic_write_memory_file(path, fm2)

    # Before TTL: still serves cache
    fake_time[0] = 5.0
    cached = cache.get(memory_id)
    assert cached is not None
    assert cached["summary"] == "test summary"

    # After TTL: re-reads from disk
    fake_time[0] = 11.0
    fresh = cache.get(memory_id)
    assert fresh is not None
    assert fresh["summary"] == "updated"


# ---------------------------------------------------------------------------
# LRU eviction: recently-used entry is retained; least-recently-used is evicted
# ---------------------------------------------------------------------------

def test_lru_eviction_retains_recently_accessed_entry(memories_dir: Path) -> None:
    """LRU semantics: re-accessing id_a after id_b makes id_b the least-recently-used.

    Sequence:
      1. cache.get(id_a) — id_a inserted (oldest)
      2. cache.get(id_b) — id_b inserted
      3. cache.get(id_a) — id_a re-accessed (now most-recently-used)
      4. cache.get(id_c) — id_c inserted; max_entries=2 → id_b evicted (LRU)

    After eviction:
      - id_a should still be cached (file removed → still returns data)
      - id_b should be evicted (file removed → returns None)
    """
    cache = MemoryMetadataCache(memories_dir, max_entries=2)

    id_a = "aaaaaaaa" * 4
    id_b = "bbbbbbbb" * 4
    id_c = "cccccccc" * 4

    path_a = _write_memory_file(memories_dir, id_a)
    path_b = _write_memory_file(memories_dir, id_b)
    _write_memory_file(memories_dir, id_c)

    cache.get(id_a)  # insert id_a (oldest)
    cache.get(id_b)  # insert id_b
    cache.get(id_a)  # re-access id_a → id_a becomes most-recently-used; id_b is now LRU
    cache.get(id_c)  # insert id_c → evicts id_b (LRU), not id_a

    # Remove both files from disk to force dependency on cache state
    path_a.unlink()
    path_b.unlink()

    # id_a: was recently accessed → still cached → should return data
    result_a = cache.get(id_a)
    assert result_a is not None, "id_a should still be cached after LRU eviction of id_b"

    # id_b: was LRU → evicted → disk gone → returns None
    result_b = cache.get(id_b)
    assert result_b is None, "id_b should have been evicted as the least-recently-used entry"


# ---------------------------------------------------------------------------
# invalidate: removes a single entry (and is idempotent)
# ---------------------------------------------------------------------------

def test_invalidate_removes_specific_entry(memories_dir: Path) -> None:
    memory_id = "feedc0de" * 4
    path = _write_memory_file(memories_dir, memory_id)

    cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    first = cache.get(memory_id)
    assert first is not None

    # Invalidate it
    cache.invalidate(memory_id)

    # Remove file so we can verify cache was cleared (not just re-reading)
    path.unlink()
    result = cache.get(memory_id)
    assert result is None


def test_invalidate_is_idempotent(memories_dir: Path) -> None:
    """invalidate on a key that was never cached should not raise."""
    cache = MemoryMetadataCache(memories_dir)
    cache.invalidate("never_cached_id")  # must not raise


# ---------------------------------------------------------------------------
# invalidate_all: clears all entries
# ---------------------------------------------------------------------------

def test_invalidate_all_clears_all_entries(memories_dir: Path) -> None:
    id_a = "aaaaaaaa" * 4
    id_b = "bbbbbbbb" * 4
    path_a = _write_memory_file(memories_dir, id_a)
    path_b = _write_memory_file(memories_dir, id_b)

    cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    cache.get(id_a)
    cache.get(id_b)

    cache.invalidate_all()

    # Remove both files; if cached, get would return them
    path_a.unlink()
    path_b.unlink()

    assert cache.get(id_a) is None
    assert cache.get(id_b) is None


# ---------------------------------------------------------------------------
# Corrupt YAML frontmatter → returns None (fail-closed)
# ---------------------------------------------------------------------------

def test_corrupt_yaml_frontmatter_returns_none(memories_dir: Path) -> None:
    """A file with a valid --- delimiter but malformed YAML body fails closed.

    The file has the opening and closing --- markers (so it is recognised as
    a frontmatter file) but the YAML content between them is deliberately
    invalid, exercising the YAML parse-failure code path.
    """
    memory_id = "bad0bad0" * 4
    path = memories_dir / f"{memory_id}.md"
    # Opening --- present, content is invalid YAML (unbalanced brace), closing --- present
    path.write_text("---\nkey: {unclosed brace\n---\nbody text\n")

    cache = MemoryMetadataCache(memories_dir)
    result = cache.get(memory_id)
    assert result is None


# ---------------------------------------------------------------------------
# referenced_repo preserved in returned dict
# ---------------------------------------------------------------------------

def test_cache_preserves_referenced_repo_field(memories_dir: Path) -> None:
    memory_id = "12341234" * 4
    _write_memory_file(memories_dir, memory_id, scope="repo", referenced_repo="my-repo")

    cache = MemoryMetadataCache(memories_dir)
    result = cache.get(memory_id)

    assert result is not None
    assert result["scope"] == "repo"
    assert result["referenced_repo"] == "my-repo"


# ---------------------------------------------------------------------------
# Thread safety smoke test: concurrent gets do not corrupt state
# ---------------------------------------------------------------------------

def test_thread_safety_smoke(memories_dir: Path) -> None:
    """Multiple threads calling get() concurrently should not raise or corrupt data.

    Each worker thread runs 20 bounded iterations. Threads are joined without
    a timeout so all must complete before we assert success.
    """
    num_ids = 10
    ids = [f"{i:08x}" * 4 for i in range(num_ids)]
    for mid in ids:
        _write_memory_file(memories_dir, mid)

    cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)

    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def _worker(mid: str) -> None:
        try:
            for _ in range(20):
                result = cache.get(mid)
                assert result is not None
                cache.invalidate(mid)
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(mid,)) for mid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()  # no timeout — bounded by 20 iterations per worker

    # Verify all threads completed (join without timeout guarantees this,
    # but we assert is_alive() == False as an explicit correctness check)
    for t in threads:
        assert not t.is_alive(), f"Thread {t.name} did not complete"

    assert not errors, f"Thread errors: {errors}"
