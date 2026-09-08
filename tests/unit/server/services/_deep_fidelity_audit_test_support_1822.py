"""Shared test support for Bug #1822 Defect 1 + follow-up (a) -- the deep-
fidelity audit's dedicated bounded ThreadPoolExecutor + gate dispatched from
FilesystemVectorStore.search(). NOT a test module itself (deliberately no
``test_`` prefix, so pytest's ``python_files = ["test_*.py"]`` config never
collects it) -- imported by the sibling Bug #1822 test files to avoid
duplicating fixture/helper setup across them.

Mirrors the patching pattern established by
tests/unit/server/services/test_audit_async_dispatch_1813.py: a small real
HNSW collection, ``coalesced_query_embedding`` patched to synthesize a
sampled on-mode HIT ``audit_ctx``, and (where a full audit run is needed)
``governed_query_embedding`` / ``HNSWIndexManager.query`` patched/spied to
observe the audit's real background work without any network call.
"""

from __future__ import annotations

import contextlib
import struct
import threading
import time
from typing import List, Optional
from unittest.mock import MagicMock, patch

import numpy as np

from code_indexer.storage import filesystem_vector_store as fvs
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

DIM = 8
EVENT_WAIT_TIMEOUT_SECS = 2.0
SEARCH_LIMIT = 3
EXECUTOR_WORKERS = 2
EXPECTED_HNSW_QUERY_CALLS = 2  # 1 primary search + 1 audit second search
REEMBED_SLEEP_SECS = 0.05
GRACE_WINDOW_SECS = 0.2  # window to catch a slipped-through call that must not happen
PROMPT_RETURN_CEILING_SECS = 0.5  # search() must return well under this when skipped
MAX_REASONABLE_GATE_CAPACITY = 64  # "small, fixed capacity" per Bug #1822
COLLECTION_NAME = "audit_capacity_coll"
AUDIT_WORKER_FN_NAME = "_run_audit_out_of_band"

ORIGINAL_HNSW_QUERY = HNSWIndexManager.query


def norm(v: List[float]) -> List[float]:
    arr = np.array(v, dtype=np.float32)
    n = np.linalg.norm(arr)
    if n == 0:
        return v
    # Explicit float() conversion (not .tolist()) so the return type is
    # genuinely List[float] with no numpy-scalar/type-ignore ambiguity.
    return [float(x) for x in (arr / n)]


def enc(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def drain_gate(gate: threading.Semaphore) -> int:
    """Non-blockingly acquire every spare permit on ``gate``; return count."""
    count = 0
    while gate.acquire(blocking=False):
        count += 1
    return count


def restore_gate(gate: threading.Semaphore, count: int) -> None:
    for _ in range(count):
        gate.release()


def assert_gate_at_full_capacity(gate: threading.Semaphore, expected: int) -> None:
    """Drain the gate to prove it currently holds exactly ``expected`` spare
    permits, then restore it -- used to verify the gate was correctly
    released back to full capacity after an audit (success or failure)."""
    drained = drain_gate(gate)
    try:
        assert drained == expected, (
            f"expected the gate to be back at full capacity ({expected}) "
            f"after the audit completed, but only {drained} permits were "
            "available -- the gate was not released"
        )
    finally:
        restore_gate(gate, drained)


def make_fake_provider(query_vec: List[float]) -> MagicMock:
    provider = MagicMock()
    provider.get_embedding.return_value = query_vec
    return provider


def make_sampled_on_mode_coalesced(query_vec: List[float]):
    """coalesced_query_embedding stand-in simulating a sampled on-mode HIT."""

    def _fake_coalesced(
        provider, text, *, no_embedding_cache_shortcut=False, audit_ctx=None
    ):
        if audit_ctx is not None:
            audit_ctx["sampled"] = True
            audit_ctx["mode"] = "on"
            audit_ctx["provider"] = "voyage-ai"
            audit_ctx["cached_blob"] = enc(query_vec)
        from code_indexer.server.services.governed_call import (
            EmbeddingCacheMetadata,
        )

        return query_vec, EmbeddingCacheMetadata(embed_key="s:d:q")

    return _fake_coalesced


def run_sampled_search(
    store,
    query_vec,
    fake_provider,
    executor,
    *,
    reembed_side_effect=None,
    hnsw_query_side_effect=None,
):
    """Invoke store.search() under a sampled on-mode HIT audit_ctx.

    Both coalesced_query_embedding and governed_query_embedding are patched
    (fast, no network / no artificial sleep by default) -- callers may
    override either patch's behavior via the side_effect params.
    """
    if reembed_side_effect is None:

        def reembed_side_effect(provider, text, *, embedding_purpose="query"):
            time.sleep(REEMBED_SLEEP_SECS)
            return query_vec

    if hnsw_query_side_effect is None:

        def hnsw_query_side_effect(hnsw_self, *args, **kwargs):
            return ORIGINAL_HNSW_QUERY(hnsw_self, *args, **kwargs)

    with (
        patch(
            "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
            side_effect=make_sampled_on_mode_coalesced(query_vec),
        ),
        patch(
            "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
            side_effect=reembed_side_effect,
        ),
        patch.object(
            HNSWIndexManager,
            "query",
            autospec=True,
            side_effect=hnsw_query_side_effect,
        ),
    ):
        t0 = time.monotonic()
        results = store.search(
            query="hello",
            embedding_provider=fake_provider,
            collection_name=COLLECTION_NAME,
            limit=SEARCH_LIMIT,
            parallel_executor=executor,
        )
        elapsed = time.monotonic() - t0

    return elapsed, results


def make_counting_hnsw_query():
    """Return (side_effect_fn, done_event, calls_dict): a real-behavior-
    preserving HNSWIndexManager.query spy that sets ``done_event`` once
    EXPECTED_HNSW_QUERY_CALLS calls have been observed (1 primary search +
    1 audit second search)."""
    done_event = threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()

    def _counting(hnsw_self, *args, **kwargs):
        result = ORIGINAL_HNSW_QUERY(hnsw_self, *args, **kwargs)
        with lock:
            calls["n"] += 1
            if calls["n"] >= EXPECTED_HNSW_QUERY_CALLS:
                done_event.set()
        return result

    return _counting, done_event, calls


def make_event_signaling_reembed(query_vec: List[float], *, sleep_secs: float = 0.0):
    """Return (side_effect_fn, started_event, finished_event) for
    governed_query_embedding, signaling start/finish via threading.Event."""
    started = threading.Event()
    finished = threading.Event()

    def _reembed(provider, text, *, embedding_purpose="query"):
        started.set()
        if sleep_secs:
            time.sleep(sleep_secs)
        finished.set()
        return query_vec

    return _reembed, started, finished


@contextlib.contextmanager
def patched_audit_sampled_search(
    query_vec: List[float],
    reembed_side_effect,
    hnsw_query_side_effect,
    *,
    audit_executor_override: Optional[MagicMock] = None,
):
    """Patch coalesced_query_embedding (sampled on-mode HIT),
    governed_query_embedding, and HNSWIndexManager.query in one context;
    optionally also override the module's dedicated audit executor."""
    with contextlib.ExitStack() as stack:
        if audit_executor_override is not None:
            stack.enter_context(
                patch.object(
                    fvs, "_deep_fidelity_audit_executor", audit_executor_override
                )
            )
        stack.enter_context(
            patch(
                "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
                side_effect=make_sampled_on_mode_coalesced(query_vec),
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
                side_effect=reembed_side_effect,
            )
        )
        stack.enter_context(
            patch.object(
                HNSWIndexManager,
                "query",
                autospec=True,
                side_effect=hnsw_query_side_effect,
            )
        )
        yield


def submitted_fn_names(spy_executor: MagicMock) -> List[Optional[str]]:
    """Names of every callable submitted to a MagicMock(wraps=ThreadPoolExecutor)."""
    return [
        getattr(call.args[0], "__name__", None)
        for call in spy_executor.submit.call_args_list
    ]


LOG_POLL_INTERVAL_SECS = 0.01


def _any_record_matches(caplog, required_substrings: List[str]) -> bool:
    """True if any captured record's (lowercased) message contains every
    (lowercased) string in ``required_substrings``. Centralizes the
    case-insensitive match used by both wait_for_log_containing and
    assert_log_contains below. Matches records at any captured level --
    callers control the level via ``caplog.at_level(...)``."""
    needles = [s.lower() for s in required_substrings]
    return any(
        all(needle in rec.message.lower() for needle in needles)
        for rec in caplog.records
    )


def wait_for_log_containing(
    caplog, required_substrings: List[str], timeout: float = EVENT_WAIT_TIMEOUT_SECS
) -> None:
    """Poll ``caplog.records`` until a matching record appears (see
    ``_any_record_matches``), or ``timeout`` elapses -- whichever first.
    Used to wait for a background thread's log call without a fixed
    sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _any_record_matches(caplog, required_substrings):
            return
        time.sleep(LOG_POLL_INTERVAL_SECS)


def assert_log_contains(caplog, required_substrings: List[str]) -> None:
    assert _any_record_matches(caplog, required_substrings), (
        f"expected a log record containing all of {required_substrings!r}; "
        f"got records: {[rec.message for rec in caplog.records]}"
    )


def assert_audit_dispatched_to_dedicated_executor(
    audit_submitted_names: List[Optional[str]],
    parallel_submitted_names: List[Optional[str]],
) -> None:
    assert AUDIT_WORKER_FN_NAME in audit_submitted_names, (
        "expected the audit worker to be submitted to the dedicated audit "
        f"executor; got submissions: {audit_submitted_names}"
    )
    assert AUDIT_WORKER_FN_NAME not in parallel_submitted_names, (
        "the audit worker was submitted to the shared parallel_executor "
        "instead of the dedicated audit executor -- this is exactly the bug "
        f"Bug #1822 Defect 1 fixes; parallel_executor submissions: "
        f"{parallel_submitted_names}"
    )
