"""
Tests for Bug #1421: intermittent "Temporal snapshot missing page N" during
multi-page snapshot reassembly.

Root cause (confirmed via code reading + 330 front-door queries, see issue
#1421 comments): read_temporal_snapshot() reads pages via SEPARATE,
non-isolated payload_cache.retrieve(key, page=n) calls in a loop. The
temporal worker (temporal_worker.py) writes grow-then-shrink checkpoints
while a query is in flight -- if the FINAL (smaller) write lands in the gap
between two of the reader's separate page-read calls, a later page index
falls out of range against the now-shrunk snapshot, raising
CacheNotFoundError -> TemporalSnapshotReassemblyError.

These tests reproduce the race DETERMINISTICALLY (no thread timing/sleep
flakiness) by using TWO real, SQLite-backed PayloadCache instances pointed
at the SAME db file (mirroring "worker" and "reader" being different call
sites in production, which they are): a plain instance used to seed/write
checkpoints, and an instrumented instance -- a thin PayloadCache subclass
whose retrieve() calls a hook at a chosen page number BEFORE delegating to
the real retrieve() -- used only for the read_temporal_snapshot() call
under test. This forces the exact interleaving the bug report describes
without any thread/sleep timing dependency. The rewrite hook itself always
writes through the PLAIN instance, never the instrumented one, so it can
never recursively re-trigger itself through store_temporal_snapshot's own
internal read-back verification.

Real SQLite-backed PayloadCache used throughout (anti-mock) -- only the
INTERLEAVING is controlled, never the storage/retrieval logic itself.

TDD: written BEFORE the fix. The reproduction test
(test_shrink_race_reproduces_bug_1421) is expected to FAIL (raise
TemporalSnapshotReassemblyError) against the pre-fix code and PASS
(transparently retry and return the correct final snapshot) once the fix
lands. The bounded-retry-exhaustion test proves the fix never loops
unboundedly (Messi #14) and that a genuinely unrecoverable failure is now
logged server-side (the issue's secondary "silent failure" finding).
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Callable

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.services.temporal_snapshot_store import (
    TemporalSnapshotReassemblyError,
    read_temporal_snapshot,
    store_temporal_snapshot,
)

_PAGE_SIZE = PayloadCacheConfig().max_fetch_size_chars

# Keeps each result entry's "content" field a fixed, comfortably-sized chunk
# so n_results scales JSON size predictably across every snapshot built by
# _snapshot() below -- large enough to force multi-page reassembly with a
# modest n_results, matching the sizing the existing #1400 test suite
# (test_temporal_snapshot_store_1400.py) already uses for the same purpose.
_CONTENT_PADDING_LEN = 95


def _snapshot(
    n_results: int, shards_completed: int, shards_total: int, tag: str = "x"
) -> dict:
    """A snapshot payload. n_results controls JSON size (and thus page
    count) -- large enough values comfortably exceed one page. `tag` is
    embedded in every result's content so that two DIFFERENT snapshot
    generations never share a byte-identical page-0 prefix -- without this,
    a splice of an old page 0 with new later pages could coincidentally
    still be syntactically valid, masking a genuine reassembly-consistency
    bug behind a lucky content match."""
    return {
        "results": [
            {
                "file_path": f"file_{i}.py",
                "content": f"{tag}{i}" + "y" * _CONTENT_PADDING_LEN,
            }
            for i in range(n_results)
        ],
        "shards_completed": shards_completed,
        "shards_total": shards_total,
        "ctx": {},
    }


class _RewriteInjectingPayloadCache(PayloadCache):
    """A thin PayloadCache subclass whose retrieve() calls a hook at a
    chosen page number BEFORE delegating to the real retrieve() --
    deterministically reproducing "a worker checkpoint write landed
    between two of the reader's separate page-read calls" without any
    thread-timing dependency. The hook is expected to write through a
    SEPARATE plain PayloadCache instance (never self) to avoid recursive
    re-triggering through store_temporal_snapshot's own read-back
    verification.
    """

    def __init__(
        self,
        *args,
        trigger_page: int,
        rewrite_fn: Callable[[], None],
        trigger_every_time: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._trigger_page = trigger_page
        self._rewrite_fn = rewrite_fn
        self._trigger_every_time = trigger_every_time
        self._fired = False
        self.rewrite_count = 0

    def retrieve(self, handle: str, page: int = 0):  # type: ignore[override]
        if page == self._trigger_page and (self._trigger_every_time or not self._fired):
            self._fired = True
            self.rewrite_count += 1
            self._rewrite_fn()
        return super().retrieve(handle, page=page)


@pytest.fixture
def cache_pair():
    """Yields (plain_cache, make_injecting_cache) -- both backed by the
    SAME temp SQLite file. plain_cache is used for all setup/seed writes
    and for rewrite hooks (never triggers the instrumented subclass).
    make_injecting_cache(trigger_page, rewrite_fn, **kw) builds the
    instrumented reader instance used only for the read_temporal_snapshot()
    call under test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "payload_cache.db"
        config = PayloadCacheConfig()

        plain = PayloadCache(db_path=db_path, config=config)
        plain.initialize()

        injecting_caches = []

        def _make_injecting(trigger_page: int, rewrite_fn, **kwargs):
            cache = _RewriteInjectingPayloadCache(
                db_path=db_path,
                config=config,
                trigger_page=trigger_page,
                rewrite_fn=rewrite_fn,
                **kwargs,
            )
            cache.initialize()
            injecting_caches.append(cache)
            return cache

        yield plain, _make_injecting

        plain.close()
        for c in injecting_caches:
            c.close()


class TestConcurrentRewriteRace:
    """Reproduces Bug #1421's grow-then-shrink checkpoint race and proves
    the fix resolves it via transparent bounded retry."""

    def test_shrink_race_reproduces_bug_1421(self, cache_pair):
        """The literal reported symptom: an in-flight large (multi-page)
        intermediate checkpoint is followed by a smaller FINAL write that
        lands exactly between the reader's page-0 and page-1 reads. Page 1
        (still expected per page-0's has_more=True) is now out of range
        against the shrunk final snapshot.

        Post-fix: read_temporal_snapshot must NOT raise -- it must detect
        the rewrite, retry the whole reassembly from page 0, and return the
        correct, complete FINAL snapshot (never a partial/stale mix).
        """
        plain, make_injecting = cache_pair
        job_id = "job-1421-shrink"

        large = _snapshot(n_results=200, shards_completed=3, shards_total=5)
        small_final = _snapshot(n_results=1, shards_completed=5, shards_total=5)
        assert len(json.dumps(large)) > _PAGE_SIZE
        assert len(json.dumps(small_final)) <= _PAGE_SIZE

        # Seed via the PLAIN cache -- no interception during setup.
        store_temporal_snapshot(plain, job_id, large, terminal=False)

        def _rewrite() -> None:
            # Simulates temporal_worker.py's unconditional FINAL write
            # (terminal=True) landing mid-read. Writes through the PLAIN
            # cache so its own internal read-back verification never
            # re-enters the instrumented subclass.
            store_temporal_snapshot(plain, job_id, small_final, terminal=True)

        reader = make_injecting(trigger_page=1, rewrite_fn=_rewrite)

        read_back = read_temporal_snapshot(reader, job_id)

        assert read_back is not None
        assert read_back["shards_completed"] == 5
        assert read_back["terminal"] is True
        assert len(read_back["results"]) == 1
        assert read_back["results"][0]["file_path"] == "file_0.py"
        # The rewrite must have actually fired mid-read to prove this test
        # exercised the race, not a no-op path.
        assert reader.rewrite_count == 1

    def test_growth_total_pages_mismatch_detected(self, cache_pair):
        """A GROWTH race: page 0 is read against a 2-page checkpoint (so
        has_more=True), then a rewrite lands making the snapshot grow to
        several pages. Page 1 of the NEW content is still a valid index (no
        CacheNotFoundError) but the total_pages metadata changed mid-read --
        this must also be detected as a rewrite (not silently spliced into
        a Frankenstein result) and retried, returning the correct, fully-
        consistent final snapshot.
        """
        plain, make_injecting = cache_pair
        job_id = "job-1421-growth"

        two_page_intermediate = _snapshot(
            n_results=90, shards_completed=2, shards_total=5, tag="old"
        )
        large_final = _snapshot(
            n_results=200, shards_completed=5, shards_total=5, tag="new"
        )
        assert len(json.dumps(two_page_intermediate)) > _PAGE_SIZE
        assert len(json.dumps(large_final)) > _PAGE_SIZE

        store_temporal_snapshot(plain, job_id, two_page_intermediate, terminal=False)

        def _rewrite() -> None:
            store_temporal_snapshot(plain, job_id, large_final, terminal=True)

        reader = make_injecting(trigger_page=1, rewrite_fn=_rewrite)

        read_back = read_temporal_snapshot(reader, job_id)

        assert read_back is not None
        assert read_back["shards_completed"] == 5
        assert read_back["terminal"] is True
        assert len(read_back["results"]) == 200
        # "old" and "new" tags are the same length, so a structurally-valid
        # splice (old page 0 bytes + new later-page bytes) can still parse
        # as JSON and pass loose count checks while silently containing
        # WRONG early-entry data -- exact per-entry comparison is required
        # to actually catch that silent corruption, not just count it away.
        assert read_back["results"] == large_final["results"]
        assert reader.rewrite_count == 1


class TestBoundedRetryExhaustionLogsError:
    """Proves the fix never retries unboundedly (Messi #14) and that a
    genuinely unrecoverable reassembly failure IS logged server-side (the
    #1421 secondary finding: the original failure produced zero log
    entries)."""

    def test_persistent_rewrite_exhausts_retries_and_raises(self, cache_pair, caplog):
        plain, make_injecting = cache_pair
        job_id = "job-1421-persistent"

        store_temporal_snapshot(
            plain, job_id, _snapshot(200, 1, 5, tag="gen0"), terminal=False
        )

        # Monotonically-growing rewrite: fires on EVERY page-1 read via the
        # PLAIN cache, each time writing a STRICTLY LARGER snapshot (more
        # results -> more pages) than whatever total_pages the reader's
        # page-0 read just observed. This guarantees the race NEVER
        # converges -- unlike rewriting identical/shrinking content (which
        # settles after one retry once the content stops changing shape),
        # a perpetually-growing writer means every single attempt observes
        # a fresh total_pages mismatch, modeling a pathological (not
        # realistic, but must be handled safely) continuously-checkpointing
        # worker and proving the bounded retry cap actually gets exercised.
        generation = iter(range(1, 100))

        def _rewrite() -> None:
            gen = next(generation)
            store_temporal_snapshot(
                plain,
                job_id,
                _snapshot(200 + gen * 60, 1, 5, tag=f"gen{gen}"),
                terminal=False,
            )

        reader = make_injecting(
            trigger_page=1, rewrite_fn=_rewrite, trigger_every_time=True
        )

        with caplog.at_level(logging.WARNING):
            with pytest.raises(TemporalSnapshotReassemblyError):
                read_temporal_snapshot(reader, job_id)

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) >= 1, "expected an ERROR-level log on exhaustion"
        assert any(job_id in r.getMessage() for r in error_records)

        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and job_id in r.getMessage()
        ]
        assert len(warning_records) >= 1, (
            "expected at least one WARNING-level log for a detected "
            "concurrent rewrite retry attempt"
        )

    def test_genuine_missing_key_returns_none_no_log_no_retry(self, cache_pair, caplog):
        """A job_id that was never written (or genuinely TTL-expired) must
        still return None immediately -- this is NOT a race and must not be
        logged as an error or retried."""
        _plain, make_injecting = cache_pair
        reader = make_injecting(trigger_page=1, rewrite_fn=lambda: None)

        with caplog.at_level(logging.WARNING):
            result = read_temporal_snapshot(reader, "does-not-exist-1421")

        assert result is None
        assert reader.rewrite_count == 0
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
