"""Bug #1538: a path-keyed HNSW cache entry must never outlive its on-disk file.

Bug #1529 made every temporal shard live at a FIXED path, so a refresh now
rewrites ``hnsw_index.bin`` IN PLACE instead of publishing a brand-new
``v_{timestamp}`` directory. The path no longer changes, so the cache key no
longer changes either -- the freshness guarantee has to come from the file
itself. ``HNSWIndexCache.get_or_load(..., index_file=...)`` already compares
the on-disk stamp against the one recorded at load time, but three holes made
that guard miss exactly the case a live cluster hits:

1. **Load-then-stamp poisoning (the indefinite-staleness root cause).** The
   stamp used to be captured AFTER ``loader()`` returned. A refresh that
   replaced the file in that window produced an entry holding the OLD graph
   stamped with the NEW file's identity, so every later HIT compared equal
   and served the pre-refresh graph FOREVER -- it could not self-heal, which
   is precisely what #1538 measured (~58% stale, still ~58% twelve minutes
   after the refresh reported success).

2. **Strictly-newer comparison.** ``>`` cannot see a replacement whose stamp
   is equal-or-older -- a same-tick rewrite, an NFS server clock skew, or a
   restored/rolled-back shard. File IDENTITY changing is the invariant, not
   time moving forward.

3. **Size and mtime are not an identity.** A refresh can rebuild a shard to
   the SAME item count (same file size) with different content; if the
   timestamp also compares equal, a size+mtime stamp matches and the stale
   entry survives indefinitely. Every ``hnsw_index.bin`` publish here is an
   atomic rename, so the inode is the exact signal that closes this.

Real hnswlib indexes, real ``os.replace``, real filesystem. The "refresh
landed mid-load" race is reproduced DETERMINISTICALLY by a loader closure
that performs the real rebuild itself between reading the file and returning
-- no sleeps, no mocking of the cache's own logic.

Typing note: the loaded index object is annotated ``Any`` throughout, the
same convention ``HNSWIndexCacheEntry.hnsw_index`` itself uses -- ``hnswlib``
is a C extension with no type stubs, so ``hnswlib.Index`` is not a usable
annotation under this project's mypy configuration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import hnswlib
import numpy as np
import pytest

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)

VECTOR_DIM = 8
MAX_ELEMENTS = 1000
EF_CONSTRUCTION = 100
HNSW_M = 16
LOAD_MAX_ELEMENTS = 100_000
CACHE_TTL_MINUTES = 60

#: Item counts standing in for "pre-refresh" and "post-refresh" shard states.
PRE_REFRESH_ITEMS = 2
POST_REFRESH_ITEMS = 5
#: Used by the equal/older-stamp cases: the replacement holds genuinely
#: different content (and therefore a different file size).
ROLLED_BACK_ITEMS = 3
BACKDATE_SECONDS = 3600

#: Vector seeds. A rebuild at the SAME item count with a DIFFERENT seed
#: produces an identically-sized file holding genuinely different vectors --
#: the case a (size, mtime) stamp cannot distinguish.
BASE_SEED = 1538
REBUILD_SEED = 24601


def _write_index(index_file: Path, item_count: int, seed: int = BASE_SEED) -> None:
    """Build ``item_count`` items and publish them the way the real
    HNSWIndexManager does: write a temp file, then atomically rename over
    the live path (``BackgroundIndexRebuilder.atomic_swap``'s sequence).

    ``seed`` chooses the vector content, so two calls with the same
    ``item_count`` and different seeds yield same-sized, different-content
    files.
    """
    if not isinstance(item_count, int) or not 1 <= item_count <= MAX_ELEMENTS:
        raise ValueError(
            f"item_count must be an int in [1, {MAX_ELEMENTS}], got {item_count!r}"
        )
    index = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
    index.init_index(
        max_elements=MAX_ELEMENTS, ef_construction=EF_CONSTRUCTION, M=HNSW_M
    )
    rng = np.random.default_rng(seed)
    index.add_items(
        rng.standard_normal((item_count, VECTOR_DIM)).astype(np.float32),
        list(range(item_count)),
    )
    staging_path = f"{index_file}.tmp"
    index.save_index(staging_path)
    os.replace(staging_path, str(index_file))


def _load_from_disk(index_file: Path) -> Any:
    index = hnswlib.Index(space="cosine", dim=VECTOR_DIM)
    index.load_index(str(index_file), max_elements=LOAD_MAX_ELEMENTS)
    return index


@pytest.fixture
def shard(tmp_path: Path) -> Tuple[HNSWIndexCache, Path, Path]:
    """A real cache plus a real on-disk index in its pre-refresh state."""
    collection_dir = tmp_path / "code-indexer-temporal-voyage_code_3-2026Q3"
    collection_dir.mkdir()
    index_file = collection_dir / "hnsw_index.bin"
    _write_index(index_file, PRE_REFRESH_ITEMS)
    cache = HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=CACHE_TTL_MINUTES))
    return cache, collection_dir, index_file


def test_refresh_landing_during_load_does_not_poison_entry(
    shard: Tuple[HNSWIndexCache, Path, Path],
) -> None:
    """A refresh that lands WHILE the loader runs must not be recorded as
    already-observed. The next read has to pick up the new graph."""
    cache, collection_dir, index_file = shard
    load_calls: List[str] = []

    def racing_loader() -> Tuple[Any, Dict[int, str]]:
        # Read the CURRENT (pre-refresh) file, exactly as the real loader does.
        index = _load_from_disk(index_file)
        # ...and only THEN does the refresh land. This is the real cluster
        # sequence: a concurrent `cidx index --index-commits` child process
        # atomically replaces the file between our read and the cache's stat.
        _write_index(index_file, POST_REFRESH_ITEMS)
        load_calls.append("racing")
        return index, {}

    first, _ = cache.get_or_load(
        str(collection_dir), racing_loader, index_file=index_file
    )
    assert first.get_current_count() == PRE_REFRESH_ITEMS
    assert load_calls == ["racing"]

    # The very next read must observe the refreshed graph, not the stale one
    # the racing load returned.
    second, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    assert second.get_current_count() == POST_REFRESH_ITEMS, (
        "cache served the pre-refresh graph after an in-place refresh -- the "
        "entry was stamped with the NEW file's identity while holding the OLD "
        "graph, so it can never self-heal (Bug #1538)"
    )


@pytest.mark.parametrize("stamp_shift_seconds", [0, -BACKDATE_SECONDS])
def test_replacement_with_equal_or_older_stamp_is_detected(
    shard: Tuple[HNSWIndexCache, Path, Path], stamp_shift_seconds: int
) -> None:
    """File identity changing -- not time moving forward -- is the invariant.

    A same-tick rewrite (shift 0) or a rolled-back / clock-skewed replacement
    (negative shift) carries an mtime that is NOT strictly newer, so a ``>``
    comparison keeps serving the superseded graph.
    """
    cache, collection_dir, index_file = shard
    _write_index(index_file, POST_REFRESH_ITEMS)

    cached, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    assert cached.get_current_count() == POST_REFRESH_ITEMS
    cached_stat = os.stat(index_file)

    # Replace with genuinely different content, then force its mtime to be
    # exactly equal to (shift 0) or older than the cached entry's stamp.
    # ns precision matters: a whole-second utime would leave the two stamps
    # unequal at ns resolution and the case would not be the intended one.
    _write_index(index_file, ROLLED_BACK_ITEMS)
    shifted_ns = cached_stat.st_mtime_ns + stamp_shift_seconds * 1_000_000_000
    os.utime(index_file, ns=(cached_stat.st_atime_ns, shifted_ns))

    replaced_stat = os.stat(index_file)
    assert replaced_stat.st_mtime_ns == shifted_ns
    assert replaced_stat.st_mtime_ns <= cached_stat.st_mtime_ns
    # Premise of the shift-0 case: with mtime pinned equal, size is the only
    # remaining signal that the file changed at all.
    assert replaced_stat.st_size != cached_stat.st_size

    reread, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    assert reread.get_current_count() == ROLLED_BACK_ITEMS, (
        "cache served the previous graph after the on-disk file was replaced "
        "with an equal-or-older stamp -- staleness detection must key on file "
        "identity, not on the stamp strictly increasing (Bug #1538)"
    )


def test_same_size_same_mtime_rebuild_is_detected(
    shard: Tuple[HNSWIndexCache, Path, Path],
) -> None:
    """A stamp of ``(mtime_ns, size)`` is NOT a file identity.

    A refresh can legitimately rebuild a shard to the SAME item count -- and
    therefore the exact same file size -- while the content genuinely differs
    (re-embedded vectors, a metadata-only change, a delta that nets out). If
    the timestamp also compares equal (a coarse server-side mtime granularity,
    a same-tick rewrite), a size+mtime stamp matches and the superseded graph
    is served forever.

    Every ``hnsw_index.bin`` publish in this codebase is an atomic rename
    (``BackgroundIndexRebuilder.atomic_swap`` and HNSWIndexManager's two
    ``os.replace`` sites -- ``_write_index`` above reproduces that exact
    sequence), so the replacement is always a DIFFERENT inode. That makes the
    detection exact rather than probabilistic, which is why the fingerprint
    must carry inode identity and not merely size and time.
    """
    cache, collection_dir, index_file = shard

    cached, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    cached_stat = os.stat(index_file)
    cached_first_vector = list(cached.get_items([0])[0])

    # Same item count => same file size, but different vector content.
    _write_index(index_file, PRE_REFRESH_ITEMS, seed=REBUILD_SEED)
    os.utime(index_file, ns=(cached_stat.st_atime_ns, cached_stat.st_mtime_ns))

    replaced_stat = os.stat(index_file)
    # Premise: the two signals a size+mtime stamp relies on are IDENTICAL...
    assert replaced_stat.st_size == cached_stat.st_size
    assert replaced_stat.st_mtime_ns == cached_stat.st_mtime_ns
    # ...while the file is genuinely a different one. This assertion also
    # proves _write_index published via an atomic rename rather than
    # rewriting the same inode in place -- the property the fix relies on.
    assert replaced_stat.st_ino != cached_stat.st_ino

    reread, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    reread_first_vector = list(reread.get_items([0])[0])
    assert reread_first_vector != cached_first_vector, (
        "cache served the superseded graph after a same-size, same-mtime "
        "in-place rebuild -- (mtime_ns, size) is not a file identity, so the "
        "fingerprint must include the inode (Bug #1538)"
    )


def test_unverifiable_freshness_never_serves_the_cached_graph(
    shard: Tuple[HNSWIndexCache, Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ "Could not check" must never be silently treated as "unchanged".

    A stat failure means freshness is UNVERIFIABLE. Serving the cached entry
    anyway is a silent failure (Messi Rule #13): it can keep returning a
    superseded graph indefinitely with no signal that anything is wrong. The
    cache must fail toward correctness -- drop the entry -- and say so.
    """
    cache, collection_dir, index_file = shard

    first, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    assert first.get_current_count() == PRE_REFRESH_ITEMS
    assert cache.get_stats().cached_repositories == 1

    # The file becomes un-stat-able (deleted here; operationally an NFS blip
    # or a permission change produces the same OSError).
    index_file.unlink()

    with caplog.at_level(logging.WARNING):
        served, id_mapping = cache.get_or_load(
            str(collection_dir),
            lambda: (_load_from_disk(index_file) if index_file.exists() else None, {}),
            index_file=index_file,
        )

    # get_or_load's documented contract for a loader that finds no index:
    # return it directly and never store an entry (EVO-64244 Facet 1).
    assert served is None, (
        "cache served a graph while freshness was unverifiable -- an "
        "unverifiable entry must be dropped, not silently trusted (Bug #1538)"
    )
    assert id_mapping == {}
    assert cache.get_stats().cached_repositories == 0, (
        "the unverifiable entry must be dropped from the cache"
    )
    assert any(record.levelno >= logging.WARNING for record in caplog.records), (
        "an unverifiable freshness check must be observable, not silent"
    )


def test_untouched_index_is_still_a_plain_cache_hit(
    shard: Tuple[HNSWIndexCache, Path, Path],
) -> None:
    """The fix must not introduce spurious reloads: an unchanged file keeps
    serving the SAME in-RAM object, with no second loader call."""
    cache, collection_dir, index_file = shard
    load_calls: List[str] = []

    def counting_loader() -> Tuple[Any, Dict[int, str]]:
        load_calls.append("load")
        return _load_from_disk(index_file), {}

    first, _ = cache.get_or_load(
        str(collection_dir), counting_loader, index_file=index_file
    )
    second, _ = cache.get_or_load(
        str(collection_dir), counting_loader, index_file=index_file
    )

    assert first is second, "unchanged index must be served from RAM"
    assert load_calls == ["load"], "unchanged index must not be re-loaded"


def test_rebuild_after_caching_is_still_detected(
    shard: Tuple[HNSWIndexCache, Path, Path],
) -> None:
    """The pre-existing (EVO-64244) guarantee stays intact: a rebuild that
    happens cleanly after the entry was cached is picked up."""
    cache, collection_dir, index_file = shard

    first, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    assert first.get_current_count() == PRE_REFRESH_ITEMS

    _write_index(index_file, POST_REFRESH_ITEMS)

    second, _ = cache.get_or_load(
        str(collection_dir),
        lambda: (_load_from_disk(index_file), {}),
        index_file=index_file,
    )
    assert second.get_current_count() == POST_REFRESH_ITEMS
