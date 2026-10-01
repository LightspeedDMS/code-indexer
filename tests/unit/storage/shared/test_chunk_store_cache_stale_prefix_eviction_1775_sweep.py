"""GitHub Bug #1775 round-3 remediation: restored proactive sweep, its
fd-count acceptance test, and bounded stale-prefix-registry growth.

See test_chunk_store_cache_stale_prefix_eviction_1775.py for the CORE
mechanism tests (invalidate_prefix, per-key staleness, thread affinity,
dedup, normalization, sibling separator guard) -- split out to respect
this project's 500-line module cap (Messi Rule #6); this file reuses that
module's ``make_versioned_snapshot`` helper.

CRITICAL finding (both an independent Claude review and an independent
Codex review, converged): round-2's per-key-only redesign fixed the O(N)
cost problem but in doing so deleted the only mechanism that actually
closed leaked handles. After a real alias swap, callers resolve to the NEW
snapshot target and never re-request the OLD path -- so a per-key-only
check (``_is_stale()`` evaluated only for the REQUESTED key) is simply
never evaluated for the old entry. It just sits in the thread's cache
until the 32-entry LRU eventually rotates it out -- the EXACT
``32 x thread-count`` mechanism that produced production's ~1260 leaked
fds in the first place. Claude's reviewer reproduced this directly: after
``invalidate_prefix()`` plus 20 subsequent real queries against a NEW
target, the OLD snapshot's fd was STILL open.

Final design: keep round-3's genuinely-good pieces (the ``set[str]`` +
O(path-depth) ``_is_stale()`` ancestor-walk for the definitive per-key
correctness check) AND restore a bounded, cursor-based sweep of the
calling thread's OWN cached entries (round-2's ORIGINAL idea, but bounded
differently than round-2's actual defect): the sweep scans only the
thread's own capped (<= 32-entry) dict against the DELTA of
newly-registered prefixes since that thread's own last sweep -- never the
full historical registry. In steady state that delta is 0 or 1, so this
is cheap; round-2's real defect was copying/scanning the FULL registry on
every MISS, which this restored sweep does not do.

Once the sweep is restored, a stale key on a cache MISS can simply be
cached NORMALLY again (the round-3-original "return uncached" branch is
removed) -- the sweep proactively re-evicts it later if unused, and the
LRU cap remains the final backstop.
"""

import os
import sqlite3

import pytest

from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from tests.unit.storage.shared.test_chunk_store_cache_stale_prefix_eviction_1775 import (
    make_versioned_snapshot,
)


@pytest.fixture
def cache():
    c = ChunkStoreThreadCache()
    yield c
    c.close_current_thread()


class TestProactiveSweepClosesEntriesTheThreadNeverExplicitlyRerequests:
    """Replaces round-2's ``TestPerKeyStalenessDoesNotProactivelySweepOtherKeys``
    (which asserted the leak surviving as intended) with the OPPOSITE
    assertion: a stale entry the thread never re-requests by name DOES get
    closed within a bounded number of subsequent calls to OTHER keys.
    """

    def test_unrelated_key_accesses_eventually_evict_a_stale_cached_entry(
        self, tmp_path, cache
    ):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        store_first = cache.get_or_open(v1_db, v1_coll)

        cache.invalidate_prefix(v1_dir)

        # A SINGLE subsequent call for a DIFFERENT key must trigger the
        # sweep and evict v1 -- the delta since this thread's last sweep
        # is exactly one new prefix (v1_dir), so this is the steady-state
        # cheap case the restored design targets.
        other_db, other_coll, _ = make_versioned_snapshot(tmp_path, "repo", "v_2", "p2")
        cache.get_or_open(other_db, other_coll)

        entries = cache._entries()
        assert (v1_db, False) not in entries, (
            "The stale v1 entry must be swept from this thread's cache "
            "after a subsequent get_or_open() call for a DIFFERENT key -- "
            "this is the mechanism that actually closes leaked fds when "
            "callers resolve to the NEW alias target and never "
            "re-request the OLD path by name (the realistic production "
            "pattern after a real alias swap)."
        )
        with pytest.raises(sqlite3.ProgrammingError):
            store_first.read("p1")

    def test_stale_cursor_does_not_move_when_nothing_new_is_registered(
        self, tmp_path, cache
    ):
        """Sanity/cost-bound check: with NO new stale registrations, the
        sweep must be a cheap no-op (cursor unchanged) -- proving the
        restored sweep is delta-based, not a full re-scan every call.
        """
        v1_db, v1_coll, _v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
        cache.get_or_open(v1_db, v1_coll)
        cursor_after_first_call = getattr(cache._local, "stale_cursor", 0)

        for i in range(5):
            other_db, other_coll, _ = make_versioned_snapshot(
                tmp_path, "repo", f"v_other_{i}", f"other_{i}"
            )
            cache.get_or_open(other_db, other_coll)

        assert getattr(cache._local, "stale_cursor", 0) == cursor_after_first_call, (
            "With no new invalidate_prefix() registrations, the sweep "
            "cursor must not advance -- it should have nothing new to "
            "consume."
        )


class TestFreshlyRecachedStaleEntryIsSweptOnNextOpportunity:
    """Round-4 code review finding (both an independent Claude review and
    an independent Codex review): when ``_is_stale()`` hits on the
    REQUESTED key, the entry is evicted+closed then re-cached normally --
    but by that same call, this thread's cursor has ALREADY consumed that
    prefix (the sweep at the top of ``get_or_open()`` runs BEFORE the
    per-key ``_is_stale()`` check), so no FUTURE sweep on this thread will
    ever re-match it again. Reachable whenever a caller deliberately
    resolves/re-requests an OLD path after invalidation -- a real, if
    lower-rate, instance of the same leak shape. Fix: a thread-local
    "pending re-check" set that the NEXT sweep drains unconditionally.
    """

    def test_freshly_recached_entry_is_evicted_on_the_very_next_sweep_opportunity(
        self, tmp_path
    ):
        cache = ChunkStoreThreadCache()
        try:
            v1_db, v1_coll, v1_dir = make_versioned_snapshot(
                tmp_path, "repo", "v_1", "p1"
            )
            cache.get_or_open(v1_db, v1_coll)
            cache.invalidate_prefix(v1_dir)

            # Deliberately RE-REQUEST the already-known-stale key -- this
            # evicts the old handle and re-caches a fresh one under the
            # SAME key, all within one call.
            recached_store = cache.get_or_open(v1_db, v1_coll)
            assert recached_store.read("p1") is not None

            entries_after_recache = cache._entries()
            assert (v1_db, False) in entries_after_recache, (
                "The re-requested key must be cached normally immediately "
                "after re-opening."
            )

            # A SINGLE subsequent call for a totally UNRELATED key -- this
            # thread never touches v1's key again by name.
            other_db, other_coll, _ = make_versioned_snapshot(
                tmp_path, "repo", "v_2", "p2"
            )
            cache.get_or_open(other_db, other_coll)

            entries_after_unrelated_call = cache._entries()
            assert (v1_db, False) not in entries_after_unrelated_call, (
                "The freshly-recached stale entry must be evicted on this "
                "thread's VERY NEXT sweep opportunity -- the cursor "
                "already consumed v1's causing prefix during the SAME "
                "call that recached it, so only an unconditional "
                "'pending re-check' drain (not the ordinary cursor-delta "
                "scan) can catch this."
            )
            with pytest.raises(sqlite3.ProgrammingError):
                recached_store.read("p1")
        finally:
            cache.close_current_thread()


class TestProactiveSweepFileDescriptorAcceptance:
    """PRIMARY acceptance evidence for the restored sweep (both an
    independent Claude review and an independent Codex review each
    independently wrote a version of this test): a dict-membership
    assertion alone is not sufficient given this exact regression already
    slipped past one before -- this counts REAL open file descriptors via
    ``/proc/self/fd``.
    """

    def test_old_snapshot_fd_closed_after_bounded_queries_against_new_targets(
        self, tmp_path, cache
    ):
        v1_db, v1_coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")

        store_first = cache.get_or_open(v1_db, v1_coll)
        assert store_first.read("p1") is not None

        fd_dir = f"/proc/{os.getpid()}/fd"
        fds_before = len(os.listdir(fd_dir))

        cache.invalidate_prefix(v1_dir)

        # Real queries against NEW targets ONLY -- the realistic
        # production pattern: callers resolve to the CURRENT alias
        # target and never re-request the superseded old path by name.
        # Well under the 32-entry LRU cap, so LRU rotation cannot explain
        # any eviction observed below -- only the restored sweep can.
        query_count = 20
        for i in range(query_count):
            other_db, other_coll, _ = make_versioned_snapshot(
                tmp_path, "repo", f"v_new_{i}", f"new_{i}"
            )
            store = cache.get_or_open(other_db, other_coll)
            assert store.read(f"new_{i}") is not None

        fds_after = len(os.listdir(fd_dir))

        entries = cache._entries()
        assert (v1_db, False) not in entries, (
            "The stale v1 entry must have been swept out of this "
            "thread's cache after enough subsequent calls, even though "
            "none of them re-requested v1 by name."
        )
        with pytest.raises(sqlite3.ProgrammingError):
            store_first.read("p1")

        # The load-bearing signal: real OS fd count must show the OLD
        # handle's fd was reclaimed, not merely left open ON TOP of the
        # 20 new ones. Strictly less than +query_count requires at least
        # one net closure (v1's) beyond the 20 new opens.
        assert fds_after < fds_before + query_count, (
            f"Real open file descriptor count grew by "
            f"{fds_after - fds_before} across {query_count} queries "
            f"against NEW targets only, expected strictly less than "
            f"{query_count} (proving the OLD snapshot's fd was actually "
            f"closed, not merely accumulated alongside the new ones -- "
            f"this is the exact production leak mechanism Bug #1775 "
            f"reports)."
        )


class TestSweepDeltaScanUsesSetForMembership:
    """Round-4 code review finding (both an independent Claude review and
    an independent Codex review): the sweep's delta scan must test
    membership against a ``set`` (O(1)), never a ``list`` (O(n)) -- Claude
    measured ~50ms on the query hot path for a resuming thread scanning a
    25,000-prefix delta against 32 cached entries with a list, vs ~0.66ms
    with a set. Tests the real production seam
    (``_stale_prefixes_delta_since``, the helper ``_sweep_stale_same_
    thread`` itself calls) directly -- no monkeypatching of internals.
    """

    def test_stale_prefixes_delta_since_returns_a_set(self, tmp_path):
        cache = ChunkStoreThreadCache()
        try:
            _db, _coll, v1_dir = make_versioned_snapshot(tmp_path, "repo", "v_1", "p1")
            cache.invalidate_prefix(v1_dir)

            delta, _new_cursor = cache._stale_prefixes_delta_since(0)

            assert isinstance(delta, set), (
                f"The delta scan container must be a set (O(1) "
                f"membership), never a list (O(n) membership) -- got "
                f"{type(delta)}."
            )
            assert delta == {v1_dir.rstrip("/")} or v1_dir in delta
        finally:
            cache.close_current_thread()


# Small cap for fast, deterministic testing -- production default is much
# larger (50,000). REGISTRATION_COUNT is exactly cap + 1 so precisely ONE
# trim fires, with a fully deterministic, known result.
MAX_TRACKED_PREFIXES = 10
REGISTRATION_COUNT = MAX_TRACKED_PREFIXES + 1
EXPECTED_RETAINED_COUNT = MAX_TRACKED_PREFIXES // 2

SELF_HEAL_CAP = 4
UNRELATED_REGISTRATIONS_PAST_CAP = 20

LOSS_REPRO_CAP = 4
LOSS_REPRO_UNRELATED_REGISTRATIONS = 10


class TestPrefixTrimmedBeforeAnySweepIsPermanentlyLost:
    """Round-4 code review finding (both an independent Claude review and
    an independent Codex review, independently reproduced -- Codex with a
    crippled cap): unlike the self-heal case in
    ``TestStalePrefixGrowthIsBounded`` (which protects the target by
    registering it LAST, so it survives trimming), if a prefix is trimmed
    away BEFORE the owning thread's cursor ever advances past it, the
    loss is total -- discarded from BOTH the ordered list (breaks the
    sweep) AND the set (breaks even a DIRECT per-key ``_is_stale()``
    re-check).

    This is the documented, ACCEPTED residual limitation (see module
    docstring's "Bounded growth" section) -- the chosen mitigation is a
    substantially raised default cap (not full watermark tracking,
    judged disproportionate complexity for the size of this gap), which
    pushes the reachable window from ~days to ~weeks/months of zero cache
    access from one specific thread combined with continuous fleet-wide
    invalidation churn.
    """

    def test_a_prefix_trimmed_before_any_sweep_ever_sees_it_is_permanently_lost(
        self, tmp_path
    ):
        cache = ChunkStoreThreadCache(max_tracked_stale_prefixes=LOSS_REPRO_CAP)
        try:
            stale_db, stale_coll, stale_dir = make_versioned_snapshot(
                tmp_path, "repo", "v_early", "p_early"
            )
            store_stale = cache.get_or_open(stale_db, stale_coll)
            assert store_stale.read("p_early") is not None

            # Register the TARGET prefix FIRST (vulnerable position),
            # then enough unrelated churn to guarantee it is trimmed away
            # before this thread ever sweeps again.
            cache.invalidate_prefix(stale_dir)
            for i in range(LOSS_REPRO_UNRELATED_REGISTRATIONS):
                cache.invalidate_prefix(f"/fake/repo/.versioned/other/v_{i}")

            assert os.path.normpath(stale_dir) not in cache._stale_prefixes, (
                "Test setup check: the churn must have actually trimmed "
                "the target prefix out of the registry for this "
                "reproduction to be meaningful."
            )

            # This thread's cursor never advanced during the churn above
            # (invalidate_prefix() never touches any thread's cursor) --
            # its next sweep opportunity is this call, for a DIFFERENT
            # key.
            other_db, other_coll, _ = make_versioned_snapshot(
                tmp_path, "repo", "v_other", "p_other"
            )
            cache.get_or_open(other_db, other_coll)

            entries = cache._entries()
            assert (stale_db, False) in entries, (
                "Documents the accepted residual limitation: a prefix "
                "trimmed away before any sweep can see it is genuinely "
                "lost -- the seeded entry survives uncollected."
            )
            assert store_stale.read("p_early") is not None, (
                "The lost entry's handle must still be open/usable -- "
                "confirming it was never evicted, not merely evicted and "
                "silently reopened."
            )
        finally:
            cache.close_current_thread()


class TestStalePrefixGrowthIsBounded:
    """Round-3 MEDIUM remediation: bound _stale_prefixes/
    _stale_prefixes_ordered growth. Both an independent Claude review and
    an independent Codex review measured unbounded growth at fleet scale
    (~980MB/year projected by Codex at 900 repos/hourly refresh). A
    configurable cap trims the OLDEST half of both structures once
    exceeded, tracked via a trim-offset so a thread whose cursor fell
    behind self-heals (re-sweeps everything currently tracked once)
    rather than silently skipping forever.
    """

    def test_registry_trims_exactly_the_oldest_half_on_overflow(self, tmp_path):
        cache = ChunkStoreThreadCache(max_tracked_stale_prefixes=MAX_TRACKED_PREFIXES)
        try:
            # Registering exactly cap + 1 prefixes triggers exactly ONE
            # trim, with a fully deterministic, known result.
            for i in range(REGISTRATION_COUNT):
                cache.invalidate_prefix(f"/fake/repo/.versioned/myrepo/v_{i}")

            assert len(cache._stale_prefixes_ordered) == EXPECTED_RETAINED_COUNT, (
                f"After exceeding the cap ({MAX_TRACKED_PREFIXES}) by "
                f"exactly one registration, the trim must snap the "
                f"ordered list down to exactly {EXPECTED_RETAINED_COUNT} "
                f"entries (half the cap) -- not merely evict one item "
                f"per overflow, and not clear/collapse to a single entry."
            )

            # The retained entries must be EXACTLY the most recently
            # registered ones (the newest half), in order -- the oldest
            # half must be dropped, not an arbitrary subset.
            expected_suffix = [
                os.path.normpath(f"/fake/repo/.versioned/myrepo/v_{i}")
                for i in range(
                    REGISTRATION_COUNT - EXPECTED_RETAINED_COUNT,
                    REGISTRATION_COUNT,
                )
            ]
            assert cache._stale_prefixes_ordered == expected_suffix, (
                "The ordered structure must retain EXACTLY the newest "
                f"{EXPECTED_RETAINED_COUNT} entries, in order, and drop "
                "exactly the oldest half."
            )
            assert cache._stale_prefixes == set(expected_suffix), (
                "The set must be trimmed in lockstep with the ordered "
                "list, to the SAME retained suffix -- otherwise memory "
                "is not actually bounded, or the two structures "
                "disagree about which prefixes are still tracked."
            )

            oldest = os.path.normpath("/fake/repo/.versioned/myrepo/v_0")
            assert oldest not in cache._stale_prefixes
        finally:
            cache.close_current_thread()

    def test_sweep_self_heals_when_thread_cursor_falls_behind_a_trim(self, tmp_path):
        """A thread whose cursor is far behind a trim boundary must still
        correctly evict a genuinely stale, currently-tracked entry -- it
        self-heals via a one-time larger re-sweep of everything currently
        tracked, rather than silently skipping forever."""
        cache = ChunkStoreThreadCache(max_tracked_stale_prefixes=SELF_HEAL_CAP)
        try:
            # Seed a REAL cached entry for a snapshot that will shortly
            # be registered stale.
            stale_db, stale_coll, stale_dir = make_versioned_snapshot(
                tmp_path, "repo", "v_stale", "p_stale"
            )
            store_stale = cache.get_or_open(stale_db, stale_coll)
            assert store_stale.read("p_stale") is not None

            # Register enough OTHER prefixes to trigger multiple trims,
            # pushing the trim-offset well past this thread's (never
            # advanced) cursor -- then register the seeded entry's OWN
            # prefix last, so it is still genuinely tracked (survives
            # trimming, being the most recent) once the sweep runs.
            for i in range(UNRELATED_REGISTRATIONS_PAST_CAP):
                cache.invalidate_prefix(f"/fake/repo/.versioned/other/v_{i}")
            cache.invalidate_prefix(stale_dir)

            # A call for a DIFFERENT key triggers this thread's sweep;
            # because its cursor is far behind the trim boundary, the
            # sweep must self-heal (re-examine everything CURRENTLY
            # tracked, including stale_dir) rather than silently skip.
            other_db, other_coll, _ = make_versioned_snapshot(
                tmp_path, "repo", "v_2", "p2"
            )
            _ = cache.get_or_open(other_db, other_coll)

            entries = cache._entries()
            assert (stale_db, False) not in entries, (
                "The self-healing sweep must still find and evict the "
                "seeded stale entry even though this thread's cursor "
                "had fallen far behind the trim boundary -- proving it "
                "re-sweeps currently-tracked prefixes rather than "
                "silently skipping them forever."
            )
            with pytest.raises(sqlite3.ProgrammingError):
                store_stale.read("p_stale")
        finally:
            cache.close_current_thread()

    def test_max_tracked_stale_prefixes_of_one_does_not_disable_invalidation(
        self, tmp_path
    ):
        """Round-4 code review floor bug: with cap=1, the OLD trim math
        was ``trim_count = len(ordered) - (cap // 2)`` -- for cap=1,
        ``cap // 2 == 0``, so registering a SECOND prefix trimmed
        `len(ordered) - 0 == len(ordered)` entries, wiping out even the
        just-added newest entry and silently disabling invalidation
        entirely from that point on. The trim must always retain at
        least the newest entry.
        """
        cache = ChunkStoreThreadCache(max_tracked_stale_prefixes=1)
        try:
            _db_a, _coll_a, dir_a = make_versioned_snapshot(
                tmp_path, "repo", "v_a", "p_a"
            )
            db_b, coll_b, dir_b = make_versioned_snapshot(
                tmp_path, "repo", "v_b", "p_b"
            )

            cache.invalidate_prefix(dir_a)
            store_b = cache.get_or_open(db_b, coll_b)
            cache.invalidate_prefix(dir_b)

            assert len(cache._stale_prefixes_ordered) >= 1, (
                "The trim must never discard the newest just-registered "
                "entry -- doing so silently disables invalidation for "
                "any prefix registered under a cap of 1."
            )
            store_b_again = cache.get_or_open(db_b, coll_b)
            assert store_b_again is not store_b, (
                "The newest registered prefix (v_b) must still be "
                "recognized as stale and evict its cached entry -- a "
                "cap of 1 must trim the OLDEST entry, never the entry "
                "just added."
            )
        finally:
            cache.close_current_thread()
