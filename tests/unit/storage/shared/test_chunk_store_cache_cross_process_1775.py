"""GitHub Bug #1775 round 5: cross-process stale-prefix propagation.

Live staging validation (real evidence, not a review nitpick) found:
single-worker solo staging passed cleanly (fd count flat across 10 real
refresh+query cycles), but 2-worker clustered staging FAILED -- fd count
grew monotonically (104->134, chunks.db handles 20->50, 11 leaked snapshot
generations), and 120 follow-up queries with no further refresh reclaimed
zero handles. Root cause: ``ChunkStoreThreadCache``'s ``_stale_prefixes``/
``_stale_prefixes_ordered`` registry lives in PER-PROCESS memory -- a
module-level singleton inside each uvicorn worker. Worker A's
``invalidate_prefix()`` (round 1-4, already correct and tested) is
invisible to worker B, a separate OS process sharing nothing in RAM.

Fix: a NEW, ADDITIVE cross-process propagation layer built on this
project's already-established cluster-aware mechanism, ``PayloadCache``
(SQLite solo / PostgreSQL cluster, CLAUDE.md's designated system for
exactly this "ephemeral cross-node data" class of problem) -- NOT a new
parallel mechanism. A single well-known registry key holds a bounded,
JSON-encoded list of recently-registered stale prefixes; ``publish_stale_
prefix()`` appends to it (best-effort, non-fatal, matching this whole
module's established fail-open philosophy); ``read_stale_prefixes_
registry()`` reads it back (handling PayloadCache's own pagination). A
background poller (mirroring ``PayloadCache.start_background_cleanup()``'s
own thread-lifecycle idiom exactly) feeds newly-discovered prefixes into
the SAME, already-proven ``ChunkStoreThreadCache.invalidate_prefix()`` --
rounds 1-4's per-key check, sweep, pending-recheck, and trim logic are
completely unchanged and untouched by this round.

This file covers the publish/read/registry-trim mechanics with a REAL
PayloadCache (SQLite-backed, single process) -- see the sibling
``test_chunk_store_cache_cross_process_multiproc_1775.py`` for the
genuine multi-OS-process reproduction that is the actual acceptance
evidence for this bug.
"""

import json

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.storage.shared.chunk_store_cache_cross_process import (
    _MAX_REGISTRY_ENTRIES,
    _REGISTRY_KEY,
    publish_stale_prefix,
    read_stale_prefixes_registry,
    reset_registry_read_failure_state,
)

FAST_TTL_SECONDS = 900
FAST_CLEANUP_INTERVAL_SECONDS = 60


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    config = PayloadCacheConfig(
        cache_ttl_seconds=FAST_TTL_SECONDS,
        cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
    )
    cache = PayloadCache(db_path=db_path, config=config)
    cache.initialize()
    yield cache
    cache.close()


CONCURRENT_PUBLISH_THREAD_JOIN_TIMEOUT_SECONDS = 10.0
PREPOPULATED_ENTRY_COUNT = _MAX_REGISTRY_ENTRIES - 10

#: Empirically measured (20/20 trials at each value): a stagger of
#: 2ms or more between two concurrent publishers against a near-cap
#: (190-entry) registry converges reliably, because the full
#: read-modify-write cycle completes in under ~2ms locally. 50ms is
#: used here (25x that measured margin) for headroom under CI load --
#: this is empirically RELIABLE under realistic timing, not a hard
#: guarantee under extreme system contention (thread scheduling and
#: SQLite I/O timing are inherently non-deterministic). It represents
#: realistic production timing (publishers on different workers/
#: machines are essentially never synchronized to sub-millisecond
#: precision) far better than a forced zero-latency back-to-back
#: thread start, which was separately measured to race in ~90% of
#: attempts locally -- an artifact of local SQLite I/O being fast
#: enough that unsynchronized-but-tight starts nearly always overlap,
#: not representative of real fleet-wide contention.
REALISTIC_STAGGER_SECONDS = 0.05


def _make_near_cap_payload_cache(tmp_path):
    """A real PayloadCache/SQLite file, prepopulated to near-cap
    occupancy -- an empty registry would make the read side of the
    race trivially fast regardless of the cap, disconnecting these
    tests from the cap-narrowing rationale they exist to prove.
    """
    config = PayloadCacheConfig(
        cache_ttl_seconds=FAST_TTL_SECONDS,
        cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
    )
    cache = PayloadCache(db_path=tmp_path / "payload_cache.db", config=config)
    cache.initialize()
    prepopulated = [
        f"/fake/repo/.versioned/myrepo/v_prepop_{i}"
        for i in range(PREPOPULATED_ENTRY_COUNT)
    ]
    cache.store_with_key(_REGISTRY_KEY, json.dumps(prepopulated))
    return cache, prepopulated


def _publish_concurrently(
    payload_cache, prefix_a: str, prefix_b: str, stagger_seconds: float
) -> "list[str]":
    """Publish two prefixes from two real threads against the SAME real
    PayloadCache -- thread B waits ``stagger_seconds`` before calling
    publish_stale_prefix(), thread A does not. Returns the final
    registry contents.
    """
    import threading
    import time

    errors: list = []
    errors_lock = threading.Lock()

    def _publish(prefix: str, delay: float) -> None:
        try:
            if delay:
                time.sleep(delay)
            publish_stale_prefix(payload_cache, prefix)
        except Exception as exc:  # pragma: no cover - failure path
            with errors_lock:
                errors.append(exc)

    thread_a = threading.Thread(target=_publish, args=(prefix_a, 0.0))
    thread_b = threading.Thread(target=_publish, args=(prefix_b, stagger_seconds))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=CONCURRENT_PUBLISH_THREAD_JOIN_TIMEOUT_SECONDS)
    thread_b.join(timeout=CONCURRENT_PUBLISH_THREAD_JOIN_TIMEOUT_SECONDS)

    assert not thread_a.is_alive() and not thread_b.is_alive(), (
        "Both publisher threads must finish within the join timeout."
    )
    assert errors == [], f"Publisher threads must not raise: {errors}"

    return read_stale_prefixes_registry(payload_cache)


class TestConcurrentPublishersUnderContention:
    """Round-6 code review (Codex found the RMW race; Claude quantified
    it and recommended shrinking the cap rather than a full redesign):
    publish_stale_prefix() performs a read-modify-write against a
    single mutable PayloadCache key -- two near-simultaneous publishers
    CAN race (both read the same "before" state, whichever write commits
    last wins, silently dropping the other's registration). Shrinking
    _MAX_REGISTRY_ENTRIES from 2,000 to 200 cuts the read side of that
    window (sequential retrieve() calls before the write) by ~10x, but
    per the coordinator's explicit accepted-tradeoff decision (no
    append-only-table redesign), this race is narrowed, NOT eliminated.

    Two complementary tests below: the realistic-timing case (which
    converges reliably under normal conditions -- empirically reliable,
    not a hard guarantee under extreme CI load, the practical claim
    this cap shrink supports) and the worst-case adversarial-timing
    case (which documents the accepted, BOUNDED safety property that
    survives even genuine contention -- no data corruption, no loss of
    unrelated entries, at most one of the two NEW entries lost).

    Round-7 SCOPE CORRECTION (Codex): the worst-case test below exercises
    and asserts EXACTLY TWO concurrent publishers -- "at least one of the
    two survives" is proven for that specific case, NOT a general
    N-writer guarantee. With 3+ concurrent publishers all reading the
    same initial registry state before any of them write, the LAST
    write can discard every earlier writer's contribution (a real,
    valid counter-scenario Codex constructed). This does not reopen the
    bounded-fallback argument: regardless of how many writers collide,
    any lost entry still degrades to exactly the same accepted
    per-process LRU fallback as a single lost entry would -- the
    WEAKER, always-true properties (no corruption, no loss of unrelated
    entries) hold at any writer count and are what this module's actual
    correctness relies on, not "at least one new entry always survives."
    """

    def test_realistic_concurrent_publishers_both_end_up_visible(self, tmp_path):
        """With a REALISTIC (small, non-adversarial) stagger between two
        concurrent publishers, both prefixes converge reliably.
        """
        cache, _prepopulated = _make_near_cap_payload_cache(tmp_path)
        try:
            prefix_a = "/fake/repo/.versioned/myrepo/v_a"
            prefix_b = "/fake/repo/.versioned/myrepo/v_b"
            registry = _publish_concurrently(
                cache, prefix_a, prefix_b, REALISTIC_STAGGER_SECONDS
            )
            assert prefix_a in registry and prefix_b in registry, (
                f"Both concurrently-published prefixes must be visible "
                f"under realistic (non-adversarial) timing -- got "
                f"{registry[-5:]} (len={len(registry)})."
            )
        finally:
            cache.close()

    def test_worst_case_adversarial_contention_never_loses_both_or_corrupts(
        self, tmp_path
    ):
        """Maximally adversarial timing (zero stagger, both threads
        started back-to-back) -- empirically measured to race in ~90%
        of attempts locally. NOT the realistic production case (see the
        sibling test above), but documents the accepted, BOUNDED
        worst-case safety property FOR EXACTLY TWO CONCURRENT
        PUBLISHERS (see the round-7 scope correction in this class's
        docstring -- NOT a general N-writer guarantee): even under a
        genuine race, ALL 190 prepopulated (unrelated) entries survive
        untouched, the registry is never corrupted, and AT LEAST ONE of
        THESE TWO new prefixes always survives -- never total loss of
        both, never loss of unrelated data. A lost registration
        degrades to round 1-4's original per-process behavior for that
        ONE entry, bounded by the existing 32-entry-per-thread LRU cap.
        """
        cache, prepopulated = _make_near_cap_payload_cache(tmp_path)
        try:
            prefix_a = "/fake/repo/.versioned/myrepo/v_a"
            prefix_b = "/fake/repo/.versioned/myrepo/v_b"
            registry = _publish_concurrently(cache, prefix_a, prefix_b, 0.0)

            assert set(prepopulated).issubset(set(registry)), (
                f"All {len(prepopulated)} unrelated prepopulated entries "
                f"must survive untouched even under a race between the "
                f"two NEW entries -- got {len(registry)} entries, "
                f"missing: {set(prepopulated) - set(registry)}."
            )
            assert len(registry) in (
                len(prepopulated) + 1,
                len(prepopulated) + 2,
            ), (
                f"Registry length must be exactly prepopulated+1 (one "
                f"new entry lost to the race) or prepopulated+2 (both "
                f"survived) -- got {len(registry)}, proving no "
                f"corruption or unrelated data loss occurred."
            )
            assert prefix_a in registry or prefix_b in registry, (
                f"With exactly THESE TWO concurrent publishers (not a "
                f"general N-writer claim -- see this class's round-7 "
                f"scope-correction docstring), AT LEAST ONE of the two "
                f"concurrently-published prefixes must survive -- total "
                f"loss of both would indicate a genuine regression, not "
                f"the accepted narrow race. Got: {registry[-5:]} "
                f"(len={len(registry)})."
            )
        finally:
            cache.close()


class TestReadEmptyRegistryReturnsEmptyList:
    def test_read_before_any_publish_returns_empty_list(self, payload_cache):
        assert read_stale_prefixes_registry(payload_cache) == []


class TestPublishThenReadRoundTrips:
    def test_single_published_prefix_is_readable(self, payload_cache):
        publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")

        registry = read_stale_prefixes_registry(payload_cache)

        assert registry == ["/fake/repo/.versioned/myrepo/v_1"]

    def test_multiple_published_prefixes_are_all_readable_in_order(self, payload_cache):
        publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")
        publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_2")
        publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_3")

        registry = read_stale_prefixes_registry(payload_cache)

        assert registry == [
            "/fake/repo/.versioned/myrepo/v_1",
            "/fake/repo/.versioned/myrepo/v_2",
            "/fake/repo/.versioned/myrepo/v_3",
        ]


class TestReadFailureStateTransitionLogging:
    """Round-7 code review (both Claude and Codex): a genuinely sustained
    PayloadCache outage was previously invisible -- round-6's fix for
    the "WARNING per 30s poll" spam concern downgraded ALL read
    failures to DEBUG, which also hid a REAL sustained outage
    indefinitely. Fix: state-transition logging -- WARNING only when a
    failure streak BEGINS or RECOVERS, DEBUG for the repeated
    failures/successes in between. Each test resets the tracked state
    via the real reset_registry_read_failure_state() function (no
    monkeypatching of private module state).
    """

    def test_first_failure_logs_a_warning(self, payload_cache, caplog):
        import logging

        reset_registry_read_failure_state()
        payload_cache.close()
        payload_cache._conn_manager = None

        with caplog.at_level(logging.WARNING):
            registry = read_stale_prefixes_registry(payload_cache)

        assert registry == []
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1, (
            f"The FIRST read failure must log exactly one WARNING -- got "
            f"{[r.message for r in warning_records]}"
        )

    def test_repeated_failures_do_not_log_additional_warnings(
        self, payload_cache, caplog
    ):
        import logging

        reset_registry_read_failure_state()
        payload_cache.close()
        payload_cache._conn_manager = None

        read_stale_prefixes_registry(payload_cache)  # first failure primes streak

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            read_stale_prefixes_registry(payload_cache)
            read_stale_prefixes_registry(payload_cache)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records == [], (
            f"Repeated failures (already in a failure streak) must NOT "
            f"log additional WARNINGs -- got "
            f"{[r.message for r in warning_records]}"
        )

    def test_recovery_after_a_failure_streak_logs_a_warning(self, tmp_path, caplog):
        import logging

        reset_registry_read_failure_state()
        config = PayloadCacheConfig(
            cache_ttl_seconds=FAST_TTL_SECONDS,
            cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
        )
        cache = PayloadCache(db_path=tmp_path / "payload_cache.db", config=config)
        cache.initialize()
        try:
            real_conn_manager = cache._conn_manager
            cache._conn_manager = None
            read_stale_prefixes_registry(cache)  # failure -- primes the streak
            cache._conn_manager = real_conn_manager

            with caplog.at_level(logging.WARNING):
                caplog.clear()
                registry = read_stale_prefixes_registry(cache)

            assert registry == []
            warning_records = [
                r for r in caplog.records if r.levelno == logging.WARNING
            ]
            assert len(warning_records) == 1, (
                f"A successful read after a failure streak must log "
                f"exactly one WARNING (recovery signal) -- got "
                f"{[r.message for r in warning_records]}"
            )
            assert "recover" in warning_records[0].message.lower()
        finally:
            cache.close()


class TestPublishReturnsSuccessSignal:
    """Round-6 code review finding (Codex): publish_stale_prefix() must
    NOT silently swallow failures -- the caller (snapshot_cache_
    invalidation.py) previously always logged "Published..." regardless
    of whether the underlying write actually succeeded, because the
    function returned None unconditionally. A real success/failure
    signal is required.
    """

    def test_publish_returns_true_on_success(self, payload_cache):
        result = publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")

        assert result is True

    def test_publish_returns_false_on_failure(self, payload_cache):
        # Close the underlying connection manager to force a genuine
        # write failure -- no mocking, a real broken PayloadCache.
        payload_cache.close()
        payload_cache._conn_manager = None

        result = publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")

        assert result is False

    def test_publish_returns_false_when_write_silently_does_not_persist(
        self, payload_cache
    ):
        """Round-7 code review (Codex): PayloadCache.store_with_key()
        returns None on BOTH success and failure -- the PostgreSQL
        backend's store() catches all exceptions internally and returns
        None either way. A bare "did the call raise" check cannot
        distinguish this. This wrapper reproduces that exact shape: a
        real PayloadCache whose store_with_key() silently no-ops
        (matching the PG backend's real swallow-and-return-None
        behavior) while retrieve()/has_key() still delegate to the REAL
        underlying store -- proving publish_stale_prefix() must verify
        the write actually landed via a read-back, not just "did the
        call complete without raising."
        """

        class _SilentlyFailingStoreCache:
            def __init__(self, real_cache):
                self._real_cache = real_cache

            def store_with_key(self, key, content):
                return None  # Silently does NOT persist -- the bug shape.

            def retrieve(self, handle, page=0):
                return self._real_cache.retrieve(handle, page=page)

            def has_key(self, key):
                return self._real_cache.has_key(key)

        wrapped = _SilentlyFailingStoreCache(payload_cache)

        # Intentional duck-typed test double (not a real PayloadCache) --
        # verifies publish_stale_prefix() handles a PayloadCache-like
        # object whose write silently does not persist.
        result = publish_stale_prefix(wrapped, "/fake/repo/.versioned/myrepo/v_1")  # type: ignore[arg-type]

        assert result is False, (
            "publish_stale_prefix() must verify the write actually "
            "landed (via a read-back) rather than trusting that "
            "store_with_key() returning without raising means success."
        )


class TestPublishDedupes:
    def test_repeated_publish_of_same_prefix_does_not_duplicate(self, payload_cache):
        for _ in range(3):
            publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")

        registry = read_stale_prefixes_registry(payload_cache)

        assert registry == ["/fake/repo/.versioned/myrepo/v_1"]


MAX_REGISTRY_ENTRIES_FOR_TEST = 10
REGISTRY_REGISTRATION_COUNT = MAX_REGISTRY_ENTRIES_FOR_TEST + 1
EXPECTED_RETAINED_COUNT = MAX_REGISTRY_ENTRIES_FOR_TEST // 2


class TestRegistryGrowthIsBounded:
    """The shared registry blob is polled and JSON-decoded by EVERY
    process/node on a schedule -- unlike the local per-process list, its
    cost scales with worker/node count too, so it must stay small.
    Trimming reuses round-4's floor-safe math (always retain at least the
    newest entry) for consistency.
    """

    def test_registry_trims_to_the_newest_half_on_overflow(self, payload_cache):
        for i in range(REGISTRY_REGISTRATION_COUNT):
            publish_stale_prefix(
                payload_cache,
                f"/fake/repo/.versioned/myrepo/v_{i}",
                max_entries=MAX_REGISTRY_ENTRIES_FOR_TEST,
            )

        registry = read_stale_prefixes_registry(payload_cache)

        assert len(registry) == EXPECTED_RETAINED_COUNT
        expected_suffix = [
            f"/fake/repo/.versioned/myrepo/v_{i}"
            for i in range(
                REGISTRY_REGISTRATION_COUNT - EXPECTED_RETAINED_COUNT,
                REGISTRY_REGISTRATION_COUNT,
            )
        ]
        assert registry == expected_suffix, (
            "The registry must retain EXACTLY the newest half, in order, "
            "dropping exactly the oldest half."
        )


class TestRegistryStoredAsJsonUnderWellKnownKey:
    def test_registry_content_is_a_json_list_under_the_well_known_key(
        self, payload_cache
    ):
        publish_stale_prefix(payload_cache, "/fake/repo/.versioned/myrepo/v_1")

        assert payload_cache.has_key(_REGISTRY_KEY)
        result = payload_cache.retrieve(_REGISTRY_KEY)
        decoded = json.loads(result.content)
        assert decoded == ["/fake/repo/.versioned/myrepo/v_1"]
