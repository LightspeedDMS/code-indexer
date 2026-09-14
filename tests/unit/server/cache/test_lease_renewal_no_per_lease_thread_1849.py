"""Bug #1849: reader-lease renewal/release must not spawn one OS thread
per lease per cleanup tick, in HNSWIndexCache, FTSIndexCache and
IdIndexCache.

Background (see CLAUDE.md "Production Scale" invariant): the cleanup
pass already runs on its own dedicated background thread
(``start_background_cleanup``). Prior to this fix, ``_renew_reader_lease_
locked``/``_release_reader_lease_locked`` each spawned a brand-new
daemon ``threading.Thread`` PER LEASE, PER TICK, to avoid doing
filesystem I/O while holding ``_cache_lock``. Because lease TTL (10 min)
vastly exceeds the cleanup interval (60s default), every live lease was
renewed -- and therefore re-threaded -- on every single tick for its
entire lifetime. ``IdIndexCache.max_entries = 200`` alone implies up to
200 thread creations per minute per uvicorn worker; ``FTSIndexCache`` is
capped by bytes, not entry count, so it is unbounded. Each thread calls
``lease.renew()``/``lease.release()``, which performs ``os.replace``/
``unlink`` against the shared cidx-meta directory -- a ``hard`` NFSv3
mount that can block FOREVER per CLAUDE.md's "Production Scale" section.
A blocked thread never exits while the next tick spawns a fresh batch
60 seconds later.

Canonical pattern this brings the three caches into line with:
``storage/shared/chunk_store_cache.py::_ensure_lease_renewal_thread`` --
ONE named thread, snapshot the lease dict under the lock, release the
lock, then act serially. That file is left untouched by this fix
(Bug #1849 AC5).

Why this test monkeypatches ``SnapshotReaderLease`` and ``threading.
Thread`` instead of using constructor injection: none of the three cache
classes accept an injectable lease factory or thread factory today --
they hard-construct ``SnapshotReaderLease(...)`` inline and call
``threading.Thread(...)`` directly, both resolved via plain module-level
name lookup. Adding an injection seam to production code is out of
scope for this bug fix (its entire premise is REMOVING thread creation,
not adding an abstraction around it -- Messi Rule 3 KISS / Rule 9
anti-divergent-creativity). The identical technique -- monkeypatching
``threading.Thread`` on the target module to observe/replace what a
lease-renewal code path constructs -- is established precedent in this
same codebase: see
``tests/unit/storage/shared/test_chunk_store_cache_lease_renewal_race_
1847_round2.py``.

Acceptance criteria covered here:

- AC1: no ``threading.Thread`` is created per lease; renewal/release run
  on the already-running background cleanup thread.
- AC2: no filesystem/NFS I/O runs while ``_cache_lock`` is held --
  PROVEN via a lease stand-in (``_LockObservingLease``) that records
  ``_cache_lock.locked()`` at the exact instant ``renew()``/``release()``
  execute, rather than merely asserted from code inspection.
- AC3: discriminating -- genuinely FAILS on the current (unfixed) code.
  With N cached, versioned-snapshot leases, the total number of Thread
  OBJECTS created across several cleanup ticks must stay at exactly one
  (the cleanup thread itself). Counts thread OBJECTS (via a real,
  functional ``threading.Thread`` subclass that records every instance
  ever constructed), never a set of thread NAMES -- CLAUDE.md's Bug
  #1650 note: threads sharing an identical name collapse under a
  set-of-names diff and silently under-report.
- AC4: leases are still renewed well inside their TTL. Default TTL here
  is 10 minutes (600s) against a 1-second test tick -- a 600x margin --
  and renewal is proven to keep firing across multiple ticks.
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Protocol, Tuple, Type

import pytest

from code_indexer.server.cache import fts_index_cache as fts_mod
from code_indexer.server.cache import hnsw_index_cache as hnsw_mod
from code_indexer.server.cache import id_index_cache as id_mod

#: Lease TTL used by every case below. Large relative to the tick
#: interval on purpose -- see AC4 margin assertion.
_TEST_TTL_MINUTES = 10.0

#: Background cleanup tick interval used by tests that actually run the
#: cleanup thread. Short so a handful of real ticks fit in a sub-5s test.
_FAST_TICK_INTERVAL_SECONDS = 1

#: Tick interval for tests that never start the cleanup thread at all
#: (the value is irrelevant there beyond satisfying config validation).
_UNUSED_TICK_INTERVAL_SECONDS = 60

#: Number of cached, versioned-snapshot leases populated per case.
_NUM_LEASES = 20

#: How long to let the real background cleanup thread run. At
#: _FAST_TICK_INTERVAL_SECONDS=1 this covers ~3-4 ticks.
_CLEANUP_OBSERVATION_SECONDS = 3.5

#: Minimum renewals a still-cached lease must have accumulated over the
#: observation window above (AC4: liveness maintained every tick).
_MIN_EXPECTED_RENEWALS = 3

#: AC4 margin: TTL (seconds) must be at least this many multiples of one
#: tick, documenting that renewal happens comfortably inside the TTL.
_MIN_TTL_TO_TICK_RATIO = 10

#: Bound on how long we wait for a stray (non-cleanup) thread to finish
#: before reading the state it wrote. Only exercised on the unfixed code
#: path, where such threads exist; on fixed code, no extra thread is
#: ever created, so this join list is always empty.
_STRAY_THREAD_JOIN_TIMEOUT_SECONDS = 2.0


class _ReaderLeaseProtocol(Protocol):
    """The subset of ``SnapshotReaderLease``'s interface the caches use.

    Documents the real shape being duck-typed by ``_LockObservingLease``
    below, since the concrete class is being fully replaced (not
    wrapped) via monkeypatch for this test.
    """

    def acquire(self) -> None: ...

    def renew(self) -> None: ...

    def release(self) -> None: ...


class _LockObservingLease:
    """Stand-in for ``SnapshotReaderLease``.

    Bug #1849 AC2: detects and RECORDS (never silently swallows) whether
    the cache's ``_cache_lock`` was held, and which thread was executing,
    at the instant ``renew()``/``release()`` ran. This is how the "no
    I/O under the lock" claim is proven rather than merely asserted from
    reading the source.

    Thread-safety note: instances of this class are read by the test's
    main thread only AFTER every thread that could still be writing to
    them has been joined (see ``_join_stray_threads`` at each call site)
    -- so no lock is needed around the plain lists below. Guaranteeing
    that ordering (rather than adding a lock that would just hide a
    still-possible read-before-write race) is the deterministic-wait
    remedy for concurrent background writes.
    """

    def __init__(self, cache_lock: threading.Lock) -> None:
        self._cache_lock = cache_lock
        self.renew_calls = 0
        self.release_calls = 0
        self.lock_held_during_renew: List[bool] = []
        self.lock_held_during_release: List[bool] = []
        self.renew_threads: List[threading.Thread] = []
        self.release_threads: List[threading.Thread] = []

    def acquire(self) -> None:
        pass

    def renew(self) -> None:
        self.renew_calls += 1
        self.lock_held_during_renew.append(self._cache_lock.locked())
        self.renew_threads.append(threading.current_thread())

    def release(self) -> None:
        self.release_calls += 1
        self.lock_held_during_release.append(self._cache_lock.locked())
        self.release_threads.append(threading.current_thread())


def _make_counting_thread_class() -> Tuple[
    Type[threading.Thread], List[threading.Thread]
]:
    """A real, fully-functional ``threading.Thread`` subclass that also
    records every instance ever constructed.

    Bug #1650 counting trap (documented in CLAUDE.md): threads spawned
    from a pool/loop often share an identical default or templated name,
    so a ``set`` of thread NAMES silently collapses distinct threads and
    under-reports. This records actual Thread OBJECTS (by identity, via
    a plain list) so nothing is collapsed.

    ``*args: Any, **kwargs: Any`` on ``__init__`` intentionally mirrors
    ``threading.Thread.__init__``'s own flexible signature (target,
    name, args, kwargs, daemon, group) -- this class must accept exactly
    whatever the real one does, since it fully replaces it for the
    duration of the test.
    """
    created: List[threading.Thread] = []

    class _CountingThread(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    return _CountingThread, created


def _join_stray_threads(
    created_threads: List[threading.Thread], cleanup_thread: threading.Thread
) -> None:
    """Deterministically wait for every non-cleanup thread to finish.

    On the unfixed code, ``_release_reader_lease_locked``/``_renew_
    reader_lease_locked`` spawn a detached daemon thread per lease;
    ``stop_background_cleanup()`` only joins the ONE named cleanup
    thread, not those. Joining every stray thread here (bounded, so a
    genuinely hung lease call cannot hang the test forever) guarantees
    all writes those threads make to a ``_LockObservingLease`` instance
    happen-before this function returns, so the caller's subsequent
    reads are race-free without needing a lock inside the lease
    stand-in. On fixed code this list is always empty (see AC3), so this
    is a no-op there.
    """
    for thread in created_threads:
        if thread is not cleanup_thread:
            thread.join(timeout=_STRAY_THREAD_JOIN_TIMEOUT_SECONDS)


def _hnsw_loader() -> Tuple[Any, Any]:
    # Return types mirror HNSWIndexCache.get_or_load's own loader
    # contract (Callable[[], Tuple[Any, Dict[int, str]]]): the first
    # element is an opaque native hnswlib index object, which this test
    # never inspects beyond identity/None-ness.
    return object(), {}


def _fts_loader() -> Tuple[Any, Any]:
    # Mirrors FTSIndexCache.get_or_load's own loader contract
    # (Callable[[], Tuple[Any, Any]]): opaque tantivy Index/Schema
    # objects from an external library with no meaningful static type
    # here.
    return object(), object()


def _id_index_loader() -> Any:
    # Mirrors IdIndexCache.get_or_load's own loader contract
    # (Callable[[], Any]): the loaded id_index is an opaque dict shape
    # this test never inspects.
    return {"k": "v"}


CACHE_CASES = [
    pytest.param(
        hnsw_mod.HNSWIndexCache,
        hnsw_mod.HNSWIndexCacheConfig,
        hnsw_mod,
        _hnsw_loader,
        "HNSWIndexCacheCleanup",
        id="hnsw",
    ),
    pytest.param(
        fts_mod.FTSIndexCache,
        fts_mod.FTSIndexCacheConfig,
        fts_mod,
        _fts_loader,
        "FTSIndexCacheCleanup",
        id="fts",
    ),
    pytest.param(
        id_mod.IdIndexCache,
        id_mod.IdIndexCacheConfig,
        id_mod,
        _id_index_loader,
        "IdIndexCacheCleanup",
        id="id_index",
    ),
]


def _build_cache_with_lease_capture(
    monkeypatch,
    tmp_path,
    cache_cls,
    config_cls,
    module,
    *,
    cleanup_interval_seconds: int,
) -> Tuple[Any, List[_LockObservingLease]]:
    """Shared setup for every case: a real cache instance wired to a real
    lease_root, with ``SnapshotReaderLease`` replaced by a lock/thread
    observing stand-in (see ``_ReaderLeaseProtocol``) so every acquired
    lease is captured.

    Returns ``(cache, observed_leases)``. ``observed_leases`` grows, in
    order, every time the cache constructs a new lease. The cache's own
    concrete type varies per parametrized case, so it is typed ``Any``
    here -- the same reason ``get_or_load`` itself is exercised only
    through its documented, cache-agnostic contract in the tests below.
    """
    lease_root = tmp_path / "cidx-meta"
    lease_root.mkdir()

    config = config_cls(
        ttl_minutes=_TEST_TTL_MINUTES,
        cleanup_interval_seconds=cleanup_interval_seconds,
    )
    cache = cache_cls(
        config=config, lease_root=lease_root, is_versioned_snapshot=lambda _p: True
    )

    observed_leases: List[_LockObservingLease] = []

    def fake_lease_factory(*_args: Any, **_kwargs: Any) -> _ReaderLeaseProtocol:
        # *_args/**_kwargs: this factory fully replaces
        # SnapshotReaderLease's constructor, whose call signature varies
        # slightly by call site (positional collection_path/ttl_seconds
        # plus keyword-only lease_root); duck-typing the replacement
        # means accepting whatever the real constructor call passes.
        lease = _LockObservingLease(cache._cache_lock)
        observed_leases.append(lease)
        return lease

    monkeypatch.setattr(module, "SnapshotReaderLease", fake_lease_factory)

    return cache, observed_leases


@pytest.mark.parametrize(
    "cache_cls,config_cls,module,loader,cleanup_thread_name", CACHE_CASES
)
def test_cleanup_ticks_do_not_spawn_per_lease_threads(
    monkeypatch, tmp_path, cache_cls, config_cls, module, loader, cleanup_thread_name
):
    """AC1 + AC2 + AC3 (discriminating) + AC4, combined.

    Populates N cached, versioned-snapshot entries (each holding a
    lock/thread-observing lease stand-in), queues one release via
    ``invalidate()``, then runs the REAL background cleanup thread across
    several ticks.
    """
    cache, observed_leases = _build_cache_with_lease_capture(
        monkeypatch,
        tmp_path,
        cache_cls,
        config_cls,
        module,
        cleanup_interval_seconds=_FAST_TICK_INTERVAL_SECONDS,
    )
    config = cache.config

    keys: List[str] = []
    for i in range(_NUM_LEASES):
        key_dir = tmp_path / f"snap_{i}"
        key_dir.mkdir()
        key = str(key_dir)
        keys.append(key)
        cache.get_or_load(key, loader)

    assert len(observed_leases) == _NUM_LEASES

    # Install the counting Thread class BEFORE any lease work that might
    # spawn a thread, so both invalidate() (release path) and the
    # cleanup ticks (renewal path) are captured.
    counting_thread_cls, created_threads = _make_counting_thread_class()
    monkeypatch.setattr(threading, "Thread", counting_thread_cls)

    # Queue a release BEFORE the cleanup thread starts, to prove release
    # is drained on the shared cleanup thread too, not a spawned one.
    cache.invalidate(keys[0])

    try:
        cache.start_background_cleanup()
        time.sleep(_CLEANUP_OBSERVATION_SECONDS)
    finally:
        cache.stop_background_cleanup()

    # --- AC3 (discriminating): count thread OBJECTS, not names ---
    # Evaluated FIRST and unconditionally: on the unfixed code this is
    # exactly the assertion that must fail (dozens of extra Thread
    # objects), independent of whether any stray thread has finished
    # executing yet.
    extra_threads = [t for t in created_threads if t is not cache._cleanup_thread]
    assert extra_threads == [], (
        "expected ZERO extra Thread objects beyond the single named "
        f"cleanup thread; created {len(extra_threads)} extra: "
        f"{[t.name for t in extra_threads]}"
    )
    assert cache._cleanup_thread is not None
    assert cache._cleanup_thread.name == cleanup_thread_name

    # Deterministically wait for any stray thread's writes to land
    # before reading lease state below (see _join_stray_threads).
    _join_stray_threads(created_threads, cache._cleanup_thread)

    # --- AC1: renew/release ran on the EXISTING cleanup thread ---
    for lease in observed_leases:
        for t in lease.renew_threads:
            assert t is cache._cleanup_thread
        for t in lease.release_threads:
            assert t is cache._cleanup_thread

    # --- AC2: proven, not asserted from inspection ---
    for lease in observed_leases:
        assert all(held is False for held in lease.lock_held_during_renew)
        assert all(held is False for held in lease.lock_held_during_release)

    # --- AC4: renewed well inside TTL ---
    assert (
        config.ttl_minutes * 60
        >= _MIN_TTL_TO_TICK_RATIO * config.cleanup_interval_seconds
    )
    still_cached_lease = observed_leases[1]  # keys[0] was invalidated/released
    assert still_cached_lease.renew_calls >= _MIN_EXPECTED_RENEWALS

    # The invalidated lease was released exactly once, off-lock, never renewed.
    released_lease = observed_leases[0]
    assert released_lease.release_calls == 1
    assert released_lease.renew_calls == 0


@pytest.mark.parametrize(
    "cache_cls,config_cls,module,loader,_cleanup_thread_name", CACHE_CASES
)
def test_stop_background_cleanup_drains_pending_release_synchronously(
    monkeypatch, tmp_path, cache_cls, config_cls, module, loader, _cleanup_thread_name
):
    """Bug #1849: a lease queued for release must not outlive the process.

    ``stop_background_cleanup()`` must process any pending release
    SYNCHRONOUSLY, even when the background cleanup thread was never
    started (so no tick would otherwise ever drain it).
    """
    cache, observed_leases = _build_cache_with_lease_capture(
        monkeypatch,
        tmp_path,
        cache_cls,
        config_cls,
        module,
        cleanup_interval_seconds=_UNUSED_TICK_INTERVAL_SECONDS,
    )

    key_dir = tmp_path / "snap"
    key_dir.mkdir()
    key = str(key_dir)
    cache.get_or_load(key, loader)
    assert len(observed_leases) == 1

    # Cleanup thread never started -- queues the release with nothing to
    # drain it, unless stop_background_cleanup() itself drains.
    cache.invalidate(key)

    cache.stop_background_cleanup()

    lease = observed_leases[0]
    assert lease.release_calls == 1
    assert lease.lock_held_during_release == [False]
