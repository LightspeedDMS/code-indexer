"""GitHub Bug #1775: ChunkStoreThreadCache file-descriptor leak fix --
CORE mechanism tests (invalidate_prefix, per-key staleness, thread
affinity, dedup, normalization, sibling separator guard).

See test_chunk_store_cache_stale_prefix_eviction_1775_sweep.py for the
round-3 proactive-sweep restoration, its fd-count acceptance test, and the
bounded-growth tests -- split out to respect this project's 500-line
module cap (Messi Rule #6).

Root cause: golden-repo refresh NEVER replaces a chunk-store file in place --
every refresh creates a brand-new ``.versioned/<repo>/v_<ts>/.../chunks.db``
path and atomically swaps the alias pointer (see
``GoldenRepoManager._cb_swap_alias``). Because the OLD snapshot's chunk-store
path never changes, ``ChunkStoreThreadCache``'s mtime-based invalidation
NEVER fires for it -- the stale cached handle (and its open sqlite3
connection / file descriptor) is held forever, bounded only by the
per-thread LRU cap (``_MAX_ENTRIES_PER_THREAD``), multiplied by thread count.

Fix: a shared, thread-safe "stale prefix" registry
(``ChunkStoreThreadCache.invalidate_prefix()``) that ONLY registers a path
prefix as stale -- it never reaches into another thread's
``threading.local()`` storage or closes a connection it does not own.
``get_or_open()`` checks the REQUESTED key against the registry via an
O(path-depth) ancestor-directory lookup (``_is_stale()``); round-3 restored
a bounded, cursor-based sweep of the thread's OWN cached entries alongside
this (see the sibling ``_sweep`` test file) so entries the thread never
re-requests by name also get proactively closed, not just entries it
happens to touch again.

Round-3 simplification: a cache MISS on a key ``_is_stale()`` flags is now
cached NORMALLY (the round-2 "return uncached" branch was removed) -- the
restored sweep proactively re-evicts it later if unused, and the LRU cap
remains the final backstop.

Every test below uses real temp-directory chunk stores built via
``ChunkStore``/``open_chunk_store_for_path`` (the same machinery production
uses) and real ``threading.Thread`` instances where thread-affinity is
under test. Closure and thread-affinity are proven through REAL,
observable behavior of the public ``ChunkStore.read()`` API -- never by
mocking ``ChunkStore.close`` or sqlite3: a closed connection raises
``sqlite3.ProgrammingError`` on the next real operation, and an untouched
connection keeps reading successfully.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR
BASELINE_UNRELATED_LOOKUPS = 5
THREAD_JOIN_TIMEOUT_SECONDS = 5.0


def make_versioned_snapshot(base: Path, repo: str, version: str, point_id: str):
    """Create a real, valid chunk store at a canonical
    ``.versioned/<repo>/<version>/.code-indexer/index/<provider>/chunks.db``
    path -- the exact layout documented in project CLAUDE.md ("Golden Repo
    Versioned Path"). Initializes it as a plain mutable ``ChunkStore``
    (matching how production actually creates a chunks.db during indexing),
    writes one real record, and closes it.

    Returns (db_path_str, collection_path_str, snapshot_dir_str). Exported
    (no leading underscore) so the sibling sweep-restoration test file can
    reuse it without duplicating this helper.
    """
    snapshot_dir = base / ".versioned" / repo / version
    collection_dir = snapshot_dir / INDEX_SUBPATH
    collection_dir.mkdir(parents=True, exist_ok=True)
    db_path = collection_dir / CHUNKS_DB_FILENAME

    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()

    return str(db_path), str(collection_dir), str(snapshot_dir)


@pytest.fixture
def cache():
    c = ChunkStoreThreadCache()
    yield c
    c.close_current_thread()


# ---------------------------------------------------------------------------
# Baseline: documents the pre-fix leak this fix closes. Passes both before
# and after the fix -- it is not itself discriminating, but it is the
# concrete evidence for "why this fix is needed" the story requires.
# ---------------------------------------------------------------------------


class TestControlBaselineLeakWithoutInvalidation:
    def test_entry_survives_many_unrelated_lookups_without_invalidate_prefix(
        self, tmp_path, cache
    ):
        v1_db, v1_coll, _v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        store_first = cache.get_or_open(v1_db, v1_coll)

        # Many subsequent get_or_open() calls for OTHER keys on the SAME
        # thread -- simulating a long-lived query-serving worker thread
        # touching many different collections over its lifetime, exactly
        # as production does. Well under the LRU cap so it isn't the
        # explanation for what we're about to assert.
        for i in range(BASELINE_UNRELATED_LOOKUPS):
            other_db, other_coll, _ = make_versioned_snapshot(
                tmp_path, "repo", f"v_other_{i}", f"other_{i}"
            )
            cache.get_or_open(other_db, other_coll)

        store_again = cache.get_or_open(v1_db, v1_coll)
        assert store_again is store_first, (
            "Baseline: without invalidate_prefix(), the stale v1 entry must "
            "still be the SAME cached object -- this is the exact leak "
            "mechanism Bug #1775 reports (a superseded snapshot's handle "
            "is held forever because its own path's mtime never changes)."
        )


# ---------------------------------------------------------------------------
# Core mechanism: invalidate_prefix() + per-key staleness check on
# get_or_open(). A stale key is evicted+reopened on direct access, and
# (round-3) cached NORMALLY -- the restored sweep (sibling test file) is
# what proactively closes it if this thread stops using it.
# ---------------------------------------------------------------------------


class TestInvalidatePrefixEvictsOnDirectAccess:
    def test_stale_entry_evicted_and_reopened_on_direct_access(self, tmp_path, cache):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")

        store_first = cache.get_or_open(v1_db, v1_coll)
        assert store_first.read("p1") is not None

        # Registers v1's snapshot dir as stale. Must be fast/safe from any
        # thread and must NOT touch entries directly.
        cache.invalidate_prefix(v1_dir)

        # Direct re-access to the SAME key: must evict+close the stale
        # handle and open a genuinely NEW one -- never reuse the closed
        # stale object.
        store_second = cache.get_or_open(v1_db, v1_coll)
        assert store_second is not store_first, (
            "get_or_open() for v1 after invalidate_prefix() must open a "
            "NEW handle, not reuse the stale one."
        )
        assert store_second.read("p1") is not None

        # Round-3: a stale key is now cached NORMALLY on re-access (the
        # restored sweep + LRU cap handle eventual eviction if this
        # thread stops using it) -- cache fixture teardown closes it.
        entries = cache._entries()
        assert (v1_db, False) in entries
        assert entries[(v1_db, False)][1] is store_second

        # Real, observable proof of closure of the OLD handle (no
        # mocking): a closed sqlite3 connection raises ProgrammingError
        # on the next real operation.
        with pytest.raises(sqlite3.ProgrammingError):
            store_first.read("p1")

    def test_invalidate_prefix_of_unrelated_path_does_not_evict(self, tmp_path, cache):
        v1_db, v1_coll, _v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        _unused_db, _unused_coll, other_dir = make_versioned_snapshot(
            tmp_path, "other-repo", "v_9", "p9"
        )

        store_first = cache.get_or_open(v1_db, v1_coll)
        cache.invalidate_prefix(other_dir)
        store_again = cache.get_or_open(v1_db, v1_coll)

        assert store_again is store_first, (
            "invalidate_prefix() for an unrelated snapshot path must not "
            "evict entries under a different prefix."
        )
        # Still fully usable -- was never touched. Still cached (this
        # branch never went stale) -- cache fixture teardown closes it.
        assert store_again.read("p1") is not None


# ---------------------------------------------------------------------------
# Thread-affinity: invalidate_prefix() + the per-key staleness check must
# NEVER cross the sqlite3 thread-affinity boundary this module's docstring
# makes MANDATORY. The assertion below is taken strictly BEFORE either
# thread performs its own legitimate self-close, so it can never be
# confused with that. (Sweep-specific thread-affinity coverage lives in
# the sibling _sweep test file.)
# ---------------------------------------------------------------------------


class TestThreadAffinityPreserved:
    def test_invalidate_prefix_never_closes_another_threads_entries(
        self, tmp_path, cache
    ):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        v2_db, v2_coll, _v2_dir = make_versioned_snapshot(tmp_path, "repo", "v_2", "p2")

        thread_a_ready = threading.Event()
        thread_b_swept = threading.Event()
        thread_a_state: dict = {}
        thread_b_state: dict = {}

        def thread_a_worker():
            store = cache.get_or_open(v1_db, v1_coll)
            thread_a_ready.set()
            thread_b_swept.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            try:
                record = store.read("p1")
                thread_a_state["read_ok"] = record is not None
            except sqlite3.ProgrammingError:
                thread_a_state["read_ok"] = False
            finally:
                cache.close_current_thread()

        thread_a = threading.Thread(target=thread_a_worker)
        thread_a.start()
        assert thread_a_ready.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

        cache.invalidate_prefix(v1_dir)

        def thread_b_worker():
            store = cache.get_or_open(v2_db, v2_coll)
            thread_b_state["read_ok"] = store.read("p2") is not None
            thread_b_swept.set()
            cache.close_current_thread()

        thread_b = threading.Thread(target=thread_b_worker)
        thread_b.start()
        thread_b.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        thread_a.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

        assert thread_b_state.get("read_ok") is True
        assert thread_a_state.get("read_ok") is True, (
            "Thread A's own connection must STILL be usable after thread "
            "B's get_or_open() (which ran after invalidate_prefix()) -- "
            "proving B never closed an entry it does not own. Crossing "
            "the sqlite3 thread-affinity boundary is exactly what this "
            "module's docstring forbids."
        )


class TestStalePrefixSetDedup:
    def test_repeated_invalidate_prefix_same_path_dedupes(self, tmp_path, cache):
        _v1_db, _v1_coll, v1_dir = make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )

        for _ in range(3):
            cache.invalidate_prefix(v1_dir)

        assert len(cache._stale_prefixes) == 1, (
            "Repeated invalidate_prefix() calls for the SAME path must "
            "dedupe -- multiple real call sites can legitimately fire for "
            "the same old_target."
        )
        assert len(cache._stale_prefixes_ordered) == 1, (
            "The ordered (cursor-sweepable) tracking structure must also "
            "dedupe -- otherwise the sweep would redundantly re-examine "
            "the same prefix multiple times."
        )

    def test_stale_prefixes_registry_is_a_set(self, cache):
        assert isinstance(cache._stale_prefixes, set), (
            "The definitive per-key correctness check must be backed by "
            "a plain set (O(1) membership, natural dedup)."
        )


class TestFreshOpenUnderStalePrefixIsCachedNormally:
    """Round-3: a brand-new db_path under an ALREADY-registered stale
    prefix (one this thread has never opened before) is now cached
    NORMALLY on open -- the restored sweep proactively re-evicts it later
    if this thread stops using it, and the LRU cap remains the final
    backstop. (Round-2's "never cache a stale entry" branch was removed.)
    """

    def test_fresh_open_under_registered_stale_prefix_is_cached_normally(
        self, tmp_path, cache
    ):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")

        cache.invalidate_prefix(v1_dir)

        store = cache.get_or_open(v1_db, v1_coll)
        assert store.read("p1") is not None

        entries = cache._entries()
        assert (v1_db, False) in entries, (
            "A fresh open against an already-registered stale prefix is "
            "now cached normally -- the sweep + LRU cap handle eventual "
            "eviction if this thread stops using it."
        )
        assert entries[(v1_db, False)][1] is store


class TestPathNormalization:
    """A stale prefix registered in a non-canonical form (trailing slash,
    double separator) must still evict -- normalization on both
    registration and lookup via os.path.normpath() (pure string, no
    filesystem access).
    """

    def test_invalidate_prefix_with_trailing_slash_still_evicts(self, tmp_path, cache):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")

        store_first = cache.get_or_open(v1_db, v1_coll)
        cache.invalidate_prefix(v1_dir + "//")

        store_second = cache.get_or_open(v1_db, v1_coll)
        assert store_second is not store_first, (
            "invalidate_prefix() must normalize a non-canonical (trailing "
            "slash / double separator) prefix so it still matches and "
            "evicts the real entry -- a silent false-negative here is a "
            "leak with no signal."
        )

    def test_invalidate_prefix_with_dot_segment_still_evicts(self, tmp_path, cache):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")

        store_first = cache.get_or_open(v1_db, v1_coll)
        non_canonical = str(Path(v1_dir).parent / "." / Path(v1_dir).name)
        cache.invalidate_prefix(non_canonical)

        store_second = cache.get_or_open(v1_db, v1_coll)
        assert store_second is not store_first, (
            "invalidate_prefix() must normalize an embedded './' segment "
            "so it still matches and evicts the real entry."
        )


def _forbid_path_resolve(*_args, **_kwargs):
    raise AssertionError(
        "Path.resolve() must never be called here -- normalization must "
        "be pure-string (os.path.normpath), never a real filesystem "
        "syscall (lstat/readlink), per CLAUDE.md's hard-NFSv3 invariant."
    )


class TestNoFilesystemSyscallsOnNormalization:
    """CLAUDE.md standing invariant: `hard` NFSv3 mounts can block a
    filesystem syscall FOREVER. Path(...).resolve() performs real
    lstat/readlink syscalls; os.path.normpath() is pure string
    manipulation. This locks in that the cache's own normalization and
    staleness-check code paths never call Path.resolve() -- monkeypatched
    to raise if invoked, exercising ONLY the cache's own methods directly
    (not through ChunkStore/open_chunk_store_for_path, which legitimately
    calls Path.resolve() elsewhere for unrelated URI-construction reasons
    on immutable opens -- that is explicitly out of scope here, so these
    tests use plain fake path strings, no real ChunkStore/sqlite3
    involved).
    """

    def test_invalidate_prefix_never_calls_path_resolve(self, cache, monkeypatch):
        monkeypatch.setattr(Path, "resolve", _forbid_path_resolve)
        cache.invalidate_prefix("/fake/repo/.versioned/myrepo//v_1/")

    def test_staleness_check_never_calls_path_resolve(self, cache, monkeypatch):
        cache.invalidate_prefix("/fake/repo/.versioned/myrepo/v_1")
        monkeypatch.setattr(Path, "resolve", _forbid_path_resolve)

        assert cache._is_stale(
            "/fake/repo/.versioned/myrepo/v_1/.code-indexer/index/coll/chunks.db"
        )
        assert not cache._is_stale("/fake/repo/.versioned/myrepo/v_2/chunks.db")


class TestSiblingVersionSeparatorGuard:
    """Production-shaped lock-in test. A stale prefix at ``.../repo/v_1``
    must NOT evict a sibling snapshot at ``.../repo/v_10/...`` -- the
    O(path-depth) ancestor-directory design proves this BY CONSTRUCTION.
    """

    def test_stale_prefix_v1_does_not_evict_sibling_v10(self, tmp_path, cache):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        v10_db, v10_coll, _v10_dir = make_versioned_snapshot(
            tmp_path, "repo", "v_10", "p10"
        )

        store_v1_first = cache.get_or_open(v1_db, v1_coll)
        store_v10 = cache.get_or_open(v10_db, v10_coll)

        cache.invalidate_prefix(v1_dir)

        store_v10_again = cache.get_or_open(v10_db, v10_coll)
        assert store_v10_again is store_v10, (
            "v_10 shares a textual prefix with v_1 but is a DIFFERENT "
            "path component -- it must survive invalidate_prefix(v_1)."
        )

        store_v1_second = cache.get_or_open(v1_db, v1_coll)
        assert store_v1_second is not store_v1_first

        entries = cache._entries()
        assert (v10_db, False) in entries
        assert (v1_db, False) in entries
        assert entries[(v1_db, False)][1] is store_v1_second

        with pytest.raises(sqlite3.ProgrammingError):
            store_v1_first.read("p1")
