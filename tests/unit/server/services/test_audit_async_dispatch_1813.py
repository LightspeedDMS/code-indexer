"""Bug #1813 DEFECT 1: a sampled cache-HIT audit must never add latency to
the caller's search() response.

Root cause: FilesystemVectorStore.search() calls
_run_deep_fidelity_audit(...) SYNCHRONOUSLY, on the calling/main thread,
AFTER the primary HNSW search and BEFORE returning results
(storage/filesystem_vector_store.py, ~line 6694). For an on-mode sampled
HIT, that audit performs a REAL, un-coalesced governed_query_embedding()
HTTP round-trip plus a second HNSW search -- entirely for telemetry
(top10_overlap) that never influences the returned results. Because the
audit fires on live staging with sample rates of 0.78-0.80, a cache HIT
routinely costs MORE wall-clock time than a MISS (which the coalescer
naturally batches/single-flights and which never triggers the audit at
all).

Fix under test: when the server passes its shared, long-lived
parallel_executor (the real production/staging configuration -- see
search_service.py's _get_query_executor()), the audit is dispatched onto
that executor and NOT awaited by the calling request. search() must return
to its caller without waiting for the audit to complete, while the audit
still genuinely runs (fire-and-forget, not silently dropped) -- including
performing its real second HNSW search.

This test exercises the REAL _run_deep_fidelity_audit implementation (the
system under test) end to end -- only its genuine external dependency, the
network-bound governed_query_embedding() re-embed call, is replaced with a
slow stand-in. coalesced_query_embedding is patched to synthesize a sampled
on-mode HIT audit_ctx, the same precondition-setup pattern already used by
tests/unit/storage/test_filesystem_vector_store_audit_1110.py, so this test
never needs the full query-embedding cache subsystem wired up.
HNSWIndexManager.query is spied on (autospec, real behavior preserved via
side_effect, all local closures -- no test-only subclass/harness class) to
prove the audit performs its own SECOND HNSW search, not just the re-embed
call.

All patches stay active until the background audit has FULLY completed
(both its re-embed call and its second HNSW search) -- the audit runs on
the shared executor's own thread, so ending the patch context too early
could let a real (unpatched) call slip through.

This test is fully self-contained (real tiny HNSW index + real
FilesystemVectorStore collection, no network calls) and proves the fix is
discriminating: it fails against the pre-fix code (search() blocks on the
artificially slow re-embed call) and passes against the fixed code
(search() returns promptly while the audit still genuinely completes both
of its real steps in the background).
"""

from __future__ import annotations

import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

_DIM = 8
_REEMBED_SLEEP_SECS = 0.6
_EVENT_WAIT_TIMEOUT_SECS = 2.0
_TEST_SEARCH_LIMIT = 3
_TEST_EXECUTOR_WORKERS = 2
_EXPECTED_HNSW_QUERY_CALLS = 2  # 1 primary search + 1 audit second search

# Real (unbound) HNSWIndexManager.query -- called from inside the spy so the
# audit's second search still does genuine work, not just gets counted.
_ORIGINAL_HNSW_QUERY = HNSWIndexManager.query


def _norm(v: List[float]) -> List[float]:
    arr = np.array(v, dtype=np.float32)
    n = np.linalg.norm(arr)
    if n == 0:
        return v
    return (arr / n).tolist()  # type: ignore[no-any-return]


def _enc(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


@pytest.fixture()
def tiny_fsv_store(tmp_path: Path):
    """A FilesystemVectorStore with a small real collection for search() integration."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
    store.create_collection("audit_async_coll", vector_size=_DIM)

    vecs = [
        _norm([1, 0, 0, 0, 0, 0, 0, 0]),
        _norm([0, 1, 0, 0, 0, 0, 0, 0]),
        _norm([0, 0, 1, 0, 0, 0, 0, 0]),
        _norm([0, 0, 0, 1, 0, 0, 0, 0]),
        _norm([0, 0, 0, 0, 1, 0, 0, 0]),
    ]
    points = [
        {
            "id": f"doc_{i}",
            "vector": vecs[i],
            "payload": {"path": f"file_{i}.py", "content": f"content {i}"},
        }
        for i in range(len(vecs))
    ]
    store.begin_indexing("audit_async_coll")
    store.upsert_points("audit_async_coll", points)
    store.end_indexing("audit_async_coll")

    return store, vecs


def _make_fake_provider(query_vec: List[float]) -> MagicMock:
    """MagicMock duck-typing the EmbeddingProvider interface (get_embedding
    only) -- no shared Protocol exists in this module to type against, and
    the real provider is never invoked directly (coalesced_query_embedding
    is patched below), so a MagicMock spy is sufficient here.
    """
    provider = MagicMock()
    provider.get_embedding.return_value = query_vec
    return provider


def _make_sampled_on_mode_coalesced(query_vec: List[float]):
    """Return a coalesced_query_embedding stand-in that simulates a sampled
    on-mode cache HIT (audit_ctx populated exactly like a real HIT would).

    This mocks the query-embedding CACHE subsystem's decision (an external
    dependency this test does not exercise), not the audit under test.
    """

    def _fake_coalesced(
        provider, text, *, no_embedding_cache_shortcut=False, audit_ctx=None
    ):
        if audit_ctx is not None:
            audit_ctx["sampled"] = True
            audit_ctx["mode"] = "on"
            audit_ctx["provider"] = "voyage-ai"
            audit_ctx["cached_blob"] = _enc(query_vec)
        from code_indexer.server.services.governed_call import (
            EmbeddingCacheMetadata,
        )

        return query_vec, EmbeddingCacheMetadata(embed_key="s:d:q")

    return _fake_coalesced


def _run_search_with_full_audit_observation(store, query_vec, fake_provider, executor):
    """Run store.search() under a sampled on-mode HIT audit_ctx with a slow
    (network-mocked) re-embed, then wait -- INSIDE the same patch context --
    for the background audit to fully complete its real workflow (re-embed
    call + its own second HNSW search).

    Local closures (not a bespoke harness class) hold the small amount of
    shared synchronization state -- one dict-based counter + three Events.

    Returns (elapsed_secs, results, hnsw_query_call_count) for the caller's
    assertions.
    """
    reembed_started = threading.Event()
    reembed_finished = threading.Event()
    second_hnsw_query_done = threading.Event()
    hnsw_query_calls = {"n": 0}
    hnsw_query_lock = threading.Lock()

    def _slow_reembed(provider, text, *, embedding_purpose="query"):
        reembed_started.set()
        time.sleep(_REEMBED_SLEEP_SECS)
        reembed_finished.set()
        return query_vec

    def _counting_hnsw_query(hnsw_self, *args, **kwargs):
        result = _ORIGINAL_HNSW_QUERY(hnsw_self, *args, **kwargs)
        with hnsw_query_lock:
            hnsw_query_calls["n"] += 1
            if hnsw_query_calls["n"] >= _EXPECTED_HNSW_QUERY_CALLS:
                second_hnsw_query_done.set()
        return result

    with (
        patch(
            "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
            side_effect=_make_sampled_on_mode_coalesced(query_vec),
        ),
        patch(
            "code_indexer.server.services.embedding_cache_audit.governed_query_embedding",
            side_effect=_slow_reembed,
        ),
        patch.object(
            HNSWIndexManager, "query", autospec=True, side_effect=_counting_hnsw_query
        ),
    ):
        t0 = time.monotonic()
        results = store.search(
            query="hello",
            embedding_provider=fake_provider,
            collection_name="audit_async_coll",
            limit=_TEST_SEARCH_LIMIT,
            parallel_executor=executor,
        )
        elapsed = time.monotonic() - t0

        # Wait for full completion INSIDE the patch context so both mocks
        # are still installed for the duration of the background audit's
        # own calls.
        assert reembed_started.wait(timeout=_EVENT_WAIT_TIMEOUT_SECS), (
            "audit's re-embed call never started"
        )
        assert reembed_finished.wait(timeout=_EVENT_WAIT_TIMEOUT_SECS), (
            "audit's re-embed call never completed"
        )
        assert second_hnsw_query_done.wait(timeout=_EVENT_WAIT_TIMEOUT_SECS), (
            "audit did not perform its own second HNSW search "
            f"(expected {_EXPECTED_HNSW_QUERY_CALLS} total "
            f"HNSWIndexManager.query calls, got {hnsw_query_calls['n']})"
        )

    return elapsed, results, hnsw_query_calls["n"]


class TestAuditDoesNotBlockSearchResponse:
    """DEFECT 1: a sampled on-mode HIT audit must not add latency to search()."""

    def test_slow_reembed_does_not_block_search_when_parallel_executor_present(
        self, tiny_fsv_store
    ):
        """With a shared server executor supplied, search() must return well
        before the audit's real (but network-mocked-slow) re-embed call
        finishes -- proving the audit runs out of band, not inline on the
        request's critical path -- while the audit still genuinely completes
        its full real workflow (re-embed + second HNSW search) afterward.
        """
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]
        fake_provider = _make_fake_provider(query_vec)

        executor = ThreadPoolExecutor(max_workers=_TEST_EXECUTOR_WORKERS)
        try:
            elapsed, results, hnsw_calls = _run_search_with_full_audit_observation(
                store, query_vec, fake_provider, executor
            )
        finally:
            executor.shutdown(wait=True)

        assert isinstance(results, list)
        assert hnsw_calls == _EXPECTED_HNSW_QUERY_CALLS
        # The search response must come back promptly -- well under the
        # re-embed's artificial sleep -- proving the audit did NOT block it.
        assert elapsed < (_REEMBED_SLEEP_SECS / 2), (
            f"search() took {elapsed:.3f}s -- the sampled audit's re-embed "
            f"call blocked the response (should return in a fraction of "
            f"the re-embed's {_REEMBED_SLEEP_SECS}s artificial latency)"
        )
