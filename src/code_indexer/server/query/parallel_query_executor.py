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

#: Issue #1516 code-review Defect 1 (HIGH): this pool is now shared
#: PROCESS-WIDE across every concurrent request's parallel-provider
#: dispatch, not a per-request cap. Each parallel-strategy query only ever
#: submits at most 2 tasks (voyage-ai + cohere) -- but under real concurrent
#: request load (e.g. MCP sync handlers dispatched via
#: `loop.run_in_executor(None, ...)` in `server/mcp/protocol.py`), many
#: queries genuinely overlap. A pool sized at 2 would queue 2N provider
#: tasks behind 2 workers for N concurrent queries, and
#: `as_completed(..., timeout=_parallel_timeout)` in
#: `semantic_query_manager.py` counts queue-wait time as provider latency
#: -- a task that never even started before the timeout gets recorded as
#: `success=False`, which can sin-bin a perfectly healthy provider purely
#: because the shared pool was saturated by UNRELATED concurrent requests.
#:
#: 64 supports 32 fully-concurrent parallel-dispatch queries with zero
#: queuing-induced false-timeout risk, and is still effectively free when
#: idle: `ThreadPoolExecutor` spawns worker threads lazily, one per
#: submitted task up to this cap, never all 64 up front. Do NOT reduce this
#: back toward 2 -- that was the exact bug this comment documents. Follows
#: the same generously-sized-static-pool convention already established by
#: `server/startup/lifespan.py`'s `_mcp_executor`
#: (`_DEFAULT_MCP_POOL_SIZE = 128`), `_query_executor`
#: (`_DEFAULT_QUERY_POOL_SIZE = 256`), and `_xray_executor`.
_DEFAULT_PARALLEL_DISPATCH_POOL_SIZE = 64

_global_parallel_query_executor_instance: Optional[ThreadPoolExecutor] = None
_global_parallel_query_executor_lock = threading.Lock()


def get_global_parallel_query_executor() -> ThreadPoolExecutor:
    """Get or create the process-wide parallel-query ThreadPoolExecutor
    singleton.

    Sized for PROCESS-WIDE aggregate concurrent load (many overlapping
    requests), NOT the per-request task count -- one parallel-strategy
    query only ever submits 2 tasks (voyage-ai + cohere), but the pool must
    have enough headroom that unrelated concurrent queries never queue
    behind each other and get misclassified as provider failures/timeouts.

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
                    max_workers=_DEFAULT_PARALLEL_DISPATCH_POOL_SIZE
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
