"""Shared, process-wide ThreadPoolExecutor for parallel provider dispatch
(Issue #1516).

Bug: `SemanticQueryManager._search_single_repository` used to construct a
brand-new `ThreadPoolExecutor(max_workers=2)` on EVERY query call, and
`shutdown()` it again at the end of that same call. This was confirmed via
strace thread-ID tracing on a real running server: a new OS thread spawns
on every single query, and dies again moments later.

This defeats Story #1492's `ChunkStoreThreadCache`
(`storage/shared/chunk_store_cache.py`) -- a `threading.local()`-based
per-thread cache of open `chunks.db` handles whose own docstring explicitly
states it is safe to share ONE instance across as many threads as needed,
"e.g. a server's shared query-executor thread pool". That cache can only
ever accumulate cross-request benefit if the SAME worker threads are
actually reused across requests, which a fresh-executor-per-query design
structurally prevents.

Concurrency safety: a `ThreadPoolExecutor` worker thread processes exactly
ONE submitted task at a time (never two tasks concurrently on the same
worker). So a task that opens a `chunks.db` handle via
`ChunkStoreThreadCache.get_or_open()` from within a task submitted to this
shared pool only ever touches that one worker thread's own
`threading.local()` slot, one task at a time -- no cross-thread sqlite3
connection sharing occurs (Story #1456 AC7's binding same-thread contract
is preserved). See
`tests/unit/server/query/test_parallel_query_executor_1516.py` for the
proof (real threads, real sqlite3, no mocking).

Singleton pattern mirrors `storage/shared/chunk_store_cache.py`'s
`get_global_chunk_store_cache()` / `reset_global_chunk_store_cache()`
exactly: a module-level `Optional[ThreadPoolExecutor]` instance guarded by
a `threading.Lock()`, double-checked locking on construction.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

#: Unchanged concurrency ceiling -- this issue is about REUSE of worker
#: threads across requests, never about raising parallelism.
_MAX_WORKERS = 2

_global_parallel_query_executor_instance: Optional[ThreadPoolExecutor] = None
_global_parallel_query_executor_lock = threading.Lock()


def get_global_parallel_query_executor() -> ThreadPoolExecutor:
    """Get or create the process-wide parallel-query ThreadPoolExecutor
    singleton.

    Returns the SAME instance on every call. Never shut down mid-request --
    callers must submit tasks and wait for their own futures, but must NOT
    call `shutdown()` on the returned executor (that would break every
    subsequent query for the lifetime of the process). Use
    `reset_global_parallel_query_executor()` for test isolation or
    graceful process shutdown instead.
    """
    global _global_parallel_query_executor_instance
    if _global_parallel_query_executor_instance is None:
        with _global_parallel_query_executor_lock:
            if _global_parallel_query_executor_instance is None:
                _global_parallel_query_executor_instance = ThreadPoolExecutor(
                    max_workers=_MAX_WORKERS
                )
    return _global_parallel_query_executor_instance


def reset_global_parallel_query_executor() -> None:
    """Reset the singleton (for test isolation, or graceful process shutdown).

    Non-blocking: shuts down the OLD instance (if any) with `wait=False` so
    a caller (e.g. a test's teardown, or the server's lifespan shutdown
    hook) never hangs waiting for straggler worker threads to finish
    whatever they were doing.
    """
    global _global_parallel_query_executor_instance
    with _global_parallel_query_executor_lock:
        old_instance = _global_parallel_query_executor_instance
        _global_parallel_query_executor_instance = None
    if old_instance is not None:
        old_instance.shutdown(wait=False)
