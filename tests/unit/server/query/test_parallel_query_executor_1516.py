"""Tests for the shared parallel-query ThreadPoolExecutor singleton (Issue #1516).

Bug: `SemanticQueryManager._search_single_repository` constructed a brand-new
`ThreadPoolExecutor(max_workers=2)` on EVERY query call (confirmed via strace
thread-ID tracing on a real running server: a new OS thread spawns per
query). This defeats Story #1492's `ChunkStoreThreadCache` -- a
`threading.local()`-based per-thread cache of open `chunks.db` handles that
can only accumulate cross-request benefit if worker threads are actually
reused across requests.

This module tests `get_global_parallel_query_executor()` /
`reset_global_parallel_query_executor()`, which mirror the exact
double-checked-locking singleton pattern used by
`storage/shared/chunk_store_cache.py`'s `get_global_chunk_store_cache()`.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from code_indexer.server.query.parallel_query_executor import (
    get_global_parallel_query_executor,
    reset_global_parallel_query_executor,
)

# Must match parallel_query_executor.py's configured worker cap exactly --
# this issue is about REUSE, never about raising the concurrency ceiling.
EXPECTED_MAX_WORKERS = 2
SEQUENTIAL_SUBMISSION_COUNT = 10
FUTURE_RESULT_TIMEOUT_SECONDS = 5


class TestSharedExecutorSingletonIdentity:
    """The accessor must return the SAME ThreadPoolExecutor instance on every
    call, never constructing a fresh pool per call."""

    def setup_method(self):
        reset_global_parallel_query_executor()

    def teardown_method(self):
        reset_global_parallel_query_executor()

    def test_returns_same_instance_across_repeated_calls(self):
        first = get_global_parallel_query_executor()
        second = get_global_parallel_query_executor()
        third = get_global_parallel_query_executor()

        assert first is second
        assert second is third

    def test_returns_a_real_threadpoolexecutor_with_expected_max_workers(self):
        executor = get_global_parallel_query_executor()

        assert isinstance(executor, ThreadPoolExecutor)
        # ThreadPoolExecutor stores the configured cap on _max_workers.
        assert executor._max_workers == EXPECTED_MAX_WORKERS

    def test_reset_causes_a_fresh_instance_on_next_access(self):
        first = get_global_parallel_query_executor()
        reset_global_parallel_query_executor()
        second = get_global_parallel_query_executor()

        assert first is not second


class TestSharedExecutorThreadReuseAcrossSequentialCalls:
    """The whole point of the fix: repeated submit-and-wait cycles through
    the shared executor must reuse the SAME small set of worker threads,
    never spawning a fresh thread per submission.
    """

    def setup_method(self):
        reset_global_parallel_query_executor()

    def teardown_method(self):
        reset_global_parallel_query_executor()

    def test_sequential_submissions_use_at_most_max_workers_distinct_threads(self):
        executor = get_global_parallel_query_executor()

        observed_thread_ids = []

        def record_thread_id() -> int:
            return threading.get_ident()

        # Submit-wait-submit-wait (never all N at once): this proves the SAME
        # 1-2 workers get reused, rather than merely observing few distinct
        # IDs because N tasks fanned out to N-of-max_workers simultaneously.
        for _ in range(SEQUENTIAL_SUBMISSION_COUNT):
            future = executor.submit(record_thread_id)
            observed_thread_ids.append(
                future.result(timeout=FUTURE_RESULT_TIMEOUT_SECONDS)
            )

        distinct_thread_ids = set(observed_thread_ids)

        assert len(distinct_thread_ids) <= EXPECTED_MAX_WORKERS, (
            f"Expected at most {EXPECTED_MAX_WORKERS} distinct worker thread "
            f"ids, got {len(distinct_thread_ids)}: {distinct_thread_ids}. A "
            f"fresh executor per submission would show up to "
            f"{SEQUENTIAL_SUBMISSION_COUNT} distinct threads."
        )
        # Never linear growth with N -- the defining symptom of the bug.
        assert len(distinct_thread_ids) < SEQUENTIAL_SUBMISSION_COUNT
