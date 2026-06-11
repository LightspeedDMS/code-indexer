"""
Shared jitter-dispatch helpers for AI-class per-target schedulers (Bug #1056).

All three thundering-herd sites in cidx-server use this module:
  - LifecycleBatchRunner._run_sub_batch
  - DependencyMapService Pass 2 per-domain loop
  - DepMapRepairExecutor Phase 3.7 per-anomaly loop

Module-level defaults are tuned for Claude CLI calls (~30s typical).

TODO: expose via Web UI Config Screen as a follow-up story when the
dispatcher proves out in production. Per CLAUDE.md hot-fix discipline,
ship the herd fix first with reasonable hard-coded defaults; the
operator can tune later through config rather than redeploy.
"""

import random
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, List, TypeVar

# Module-level jitter defaults (seconds).
DEFAULT_LIFECYCLE_DISPATCH_JITTER_SECONDS = 2.0
DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS = 2.0
DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS = 2.0

T = TypeVar("T")
R = TypeVar("R")


def dispatch_parallel_with_jitter(
    items: List[T],
    *,
    concurrency: int,
    base_jitter_seconds: float,
    worker_fn: Callable[[T], R],
) -> List[Future]:  # type: ignore[type-arg]
    """Submit items to a ThreadPoolExecutor; each worker thread sleeps for
    random.uniform(0, base_jitter_seconds) BEFORE invoking worker_fn.

    Returns Futures in input order. The pool is shut down with wait=False
    before return so callers can iterate via as_completed.

    Idempotency: base_jitter_seconds <= 0 disables jitter (worker_fn is
    called immediately) — matches the pre-bug behaviour for clean fallback
    in tests and emergency operator override.

    Threading: jitter is paid by the WORKER thread, not the submitter, so
    even queued tasks waiting on a busy pool get smoothed when they finally
    run.
    """
    jitter = float(base_jitter_seconds)

    def _jittered(item: T) -> R:
        if jitter > 0:
            time.sleep(random.uniform(0, jitter))
        return worker_fn(item)

    pool = ThreadPoolExecutor(max_workers=concurrency)
    futures: List[Future] = [pool.submit(_jittered, item) for item in items]  # type: ignore[type-arg]
    pool.shutdown(wait=False)
    return futures


def sleep_with_jitter(base_jitter_seconds: float) -> None:
    """Sleep for random.uniform(0, base_jitter_seconds).

    For sequential per-iteration code paths between Claude calls
    (dep-map Pass 2 loop, Phase 3.7 anomaly loop).

    No-op when base_jitter_seconds <= 0.
    """
    if base_jitter_seconds > 0:
        time.sleep(random.uniform(0, float(base_jitter_seconds)))
