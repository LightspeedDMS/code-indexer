"""Bug #1538: a path-keyed HNSW cache entry must never outlive its on-disk file.

Bug #1529 made every temporal shard live at a FIXED path, so a refresh now
rewrites ``hnsw_index.bin`` IN PLACE instead of publishing a brand-new
``v_{timestamp}`` directory. The path no longer changes, so the cache key no
longer changes either -- the freshness guarantee has to come from the file
itself. ``HNSWIndexCache.get_or_load(..., index_file=...)`` compares the
on-disk identity against the one recorded at load time. Five properties make
that safe, each pinned by a test below:

1. **Load-then-stamp poisoning (the indefinite-staleness root cause).** The
   stamp must be captured BEFORE ``loader()`` runs. A refresh replacing the
   file in that window would otherwise produce an entry holding the OLD graph
   stamped with the NEW file's identity, so every later HIT compares equal and
   serves the pre-refresh graph FOREVER -- exactly what #1538 measured (~58%
   stale, unchanged twelve minutes after the refresh reported success).

2. **Identity, not "newer".** ``>`` cannot see a replacement whose stamp is
   equal-or-older -- a same-tick rewrite, an NFS clock skew, a restored shard.

3. **Size and mtime are not an identity.** A refresh can rebuild a shard to the
   same item count (same size) with different content; if the timestamp also
   compares equal, a size+mtime stamp matches and the stale entry survives.
   Every ``hnsw_index.bin`` publish here is an atomic rename, so the inode is
   the exact signal that closes this.

4. **The check must not hold the cache lock.** The golden-repos mount is
   ``hard`` NFSv3, where an outage makes ``os.stat()`` block in uninterruptible
   kernel retry rather than fail. Running that under the shared ``_cache_lock``
   would let one blocked stat stall every other cache consumer in the worker.

5. **An unverifiable check must be BOUNDED.** If ``stat()`` keeps failing while
   the loader keeps succeeding, dropping the entry on every hit thrashes
   forever (a reload and a WARNING per query, indefinitely).

Real hnswlib indexes, real ``os.replace``, real filesystem, real threads. The
"refresh landed mid-load" race is reproduced DETERMINISTICALLY by a loader
closure that performs the real rebuild itself between reading the file and
returning -- no sleeps, no mocking of the cache's own logic.

Where a test needs ``os.stat()`` to block or fail on demand (properties 4 and
5), it substitutes the module-level ``_stat_index_fingerprint`` helper -- the
single OS I/O boundary, not the cache logic under test. The only faithful
alternative would be a genuinely hung NFS server, which a unit test cannot
provision; the cache's own decision-making is exercised for real throughout.

Typing note: the loaded index object is annotated ``Any`` throughout, the same
convention ``HNSWIndexCacheEntry.hnsw_index`` itself uses -- ``hnswlib`` is a C
extension with no type stubs, so ``hnswlib.Index`` is not a usable annotation
under this project's mypy configuration.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import hnswlib
import numpy as np
import pytest

from code_indexer.server.cache import hnsw_index_cache
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

#: Generous bounds for the concurrency test: long enough that a slow machine
#: never trips them, short enough that a genuine deadlock fails fast.
LOCK_CONTENTION_TIMEOUT_SECONDS = 5.0
THREAD_JOIN_TIMEOUT_SECONDS = 30.0

#: How many HITs the bounded-unverifiable test drives while stat() stays broken.
UNVERIFIABLE_HIT_COUNT = 5


def _write_index(index_file: Path, item_count: int, seed: int = BASE_SEED) -> None:
    """Build ``item_count`` items and publish them the way the real
    HNSWIndexManager does: write a temp file, then atomically rename over the
    live path (``BackgroundIndexRebuilder.atomic_swap``'s sequence).

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


def _make_shard(parent: Path, name: str) -> Tuple[Path, Path]:
    """Create a real collection directory holding a real pre-refresh index."""
    collection_dir = parent / name
    collection_dir.mkdir()
    index_file = collection_dir / "hnsw_index.bin"
    _write_index(index_file, PRE_REFRESH_ITEMS)
    return collection_dir, index_file


@pytest.fixture
def shard(tmp_path: Path) -> Tuple[HNSWIndexCache, Path, Path]:
    """A real cache plus a real on-disk index in its pre-refresh state."""
    collection_dir, index_file = _make_shard(
        tmp_path, "code-indexer-temporal-voyage_code_3-2026Q3"
    )
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


def test_freshness_check_does_not_hold_the_cache_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked freshness stat must not stall the whole cache.

    The golden-repos mount is ``hard`` NFSv3: during a server outage
    ``os.stat()`` blocks in uninterruptible kernel retry instead of failing.
    If that ran under the shared ``_cache_lock``, one blocked stat would
    serialize every other cache consumer in the worker behind it -- a real
    availability regression introduced by the freshness mechanism itself.

    Two independent collections, two threads: while the first key's stat is
    blocked, the second key must still be served.
    """
    cache = HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=CACHE_TTL_MINUTES))
    blocked_dir, blocked_file = _make_shard(tmp_path, "collection-blocked")
    other_dir, other_file = _make_shard(tmp_path, "collection-other")

    def blocked_loader() -> Tuple[Any, Dict[int, str]]:
        return _load_from_disk(blocked_file), {}

    def other_loader() -> Tuple[Any, Dict[int, str]]:
        return _load_from_disk(other_file), {}

    # Populate both entries while stat still behaves normally.
    cache.get_or_load(str(blocked_dir), blocked_loader, index_file=blocked_file)
    cache.get_or_load(str(other_dir), other_loader, index_file=other_file)

    real_stat = hnsw_index_cache._stat_index_fingerprint
    inside_stat = threading.Event()
    release_stat = threading.Event()

    def hanging_stat(index_file: Path) -> Optional[Tuple[int, int, int, int]]:
        """Stand in for a stat blocked on a hung hard-NFS mount."""
        if Path(index_file) == blocked_file:
            inside_stat.set()
            assert release_stat.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        # real_stat is read off the module, so it is typed Any; bind it to a
        # declared local rather than widening this function's return type.
        fingerprint: Optional[Tuple[int, int, int, int]] = real_stat(index_file)
        return fingerprint

    monkeypatch.setattr(hnsw_index_cache, "_stat_index_fingerprint", hanging_stat)

    def read_blocked() -> None:
        cache.get_or_load(str(blocked_dir), blocked_loader, index_file=blocked_file)

    other_served = threading.Event()

    def read_other() -> None:
        cache.get_or_load(str(other_dir), other_loader, index_file=other_file)
        other_served.set()

    blocked_thread = threading.Thread(target=read_blocked)
    blocked_thread.start()
    try:
        assert inside_stat.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS), (
            "the blocked key's freshness stat never ran"
        )

        other_thread = threading.Thread(target=read_other)
        other_thread.start()
        served_in_time = other_served.wait(timeout=LOCK_CONTENTION_TIMEOUT_SECONDS)
    finally:
        release_stat.set()
        blocked_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        other_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    assert served_in_time, (
        "a second collection could not be served while another key's freshness "
        "stat was blocked -- the potentially-blocking stat is running under the "
        "shared cache lock, so one hung NFS call stalls every cache consumer "
        "in the worker (Bug #1538)"
    )


def test_unverifiable_freshness_is_bounded(
    shard: Tuple[HNSWIndexCache, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A permanently-failing stat must not thrash on every hit.

    With a broken stat and a WORKING loader, dropping the entry each time
    reloads and warns on every single query for as long as the stat stays
    broken -- unbounded, and it never settles. The degraded state itself has
    to be bounded: back off from re-attempting, and say so ONCE rather than
    once per hit.

    Dropping also cannot produce fresher data here: if the file is genuinely
    unreachable the loader cannot read it either, so thrashing buys nothing.
    """
    cache, collection_dir, index_file = shard
    load_calls: List[str] = []

    def counting_loader() -> Tuple[Any, Dict[int, str]]:
        load_calls.append("load")
        return _load_from_disk(index_file), {}

    first, _ = cache.get_or_load(
        str(collection_dir), counting_loader, index_file=index_file
    )
    assert load_calls == ["load"]

    # From here on the freshness check can never succeed, while the loader
    # would still succeed every time.
    def failing_stat(index_file: Path) -> Optional[Tuple[int, int, int, int]]:
        return None

    monkeypatch.setattr(hnsw_index_cache, "_stat_index_fingerprint", failing_stat)

    with caplog.at_level(logging.WARNING):
        for _ in range(UNVERIFIABLE_HIT_COUNT):
            served, _ = cache.get_or_load(
                str(collection_dir), counting_loader, index_file=index_file
            )
            assert served is not None

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(load_calls) < 1 + UNVERIFIABLE_HIT_COUNT, (
        f"loader ran {len(load_calls)} times across {UNVERIFIABLE_HIT_COUNT} "
        "hits with a permanently-failing stat -- the degraded state re-attempts "
        "on every hit instead of backing off (Bug #1538)"
    )
    assert len(warnings) == 1, (
        f"emitted {len(warnings)} warnings across {UNVERIFIABLE_HIT_COUNT} "
        "hits -- an unverifiable entry must be reported once per degradation, "
        "not once per query (Bug #1538)"
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
