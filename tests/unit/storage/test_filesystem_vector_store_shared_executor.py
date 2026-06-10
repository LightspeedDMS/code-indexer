"""Shared long-lived query executor injection at the leaf hot path.

Performance refactor (perf): FilesystemVectorStore.search() created a fresh
ThreadPoolExecutor(max_workers=2) PER REQUEST to run the embed-task in parallel
with the index-load task. Under concurrent server load, the per-request
create/destroy churn dominated CPython's process-wide _global_shutdown_lock
(71% of worker-thread samples in py-spy `submit`). The fix lets the server pass
a SHARED, long-lived executor down to search(); the CLI/solo/daemon path
(single-user, not concurrent) keeps the current per-call executor.

Behaviour-preserving contract:
- search(parallel_executor=<shared pool>)  -> submits to the shared pool,
  does NOT construct a new ThreadPoolExecutor, does NOT shut the pool down.
- search()  (no parallel_executor)         -> unchanged: builds a per-call
  ThreadPoolExecutor(max_workers=2) exactly as before (CLI behaviour).
- Identical results, identical exception propagation either way.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _make_store_with_data(tmp_path: Path, vector_size: int = 64, n: int = 10):
    store = FilesystemVectorStore(tmp_path, project_root=tmp_path)
    store.create_collection("test_collection", vector_size=vector_size)
    points = [
        {
            "id": f"vec_{i}",
            "vector": np.random.randn(vector_size).tolist(),
            "payload": {"path": f"file_{i}.py", "content": f"def f_{i}(): pass"},
        }
        for i in range(n)
    ]
    store.begin_indexing("test_collection")
    store.upsert_points("test_collection", points)
    store.end_indexing("test_collection")
    return store


class TestServerPathReusesSharedExecutor:
    """When a shared executor is injected, search() must reuse it, not create one."""

    def test_injected_executor_receives_submits_and_no_new_pool_constructed(
        self, tmp_path: Path
    ):
        """Server path: submits go to the injected pool; no new ThreadPoolExecutor.

        Asserts the hot-path churn is eliminated: with a shared executor injected,
        search() must NOT construct a fresh ThreadPoolExecutor.
        """
        store = _make_store_with_data(tmp_path)

        mock_provider = Mock()
        mock_provider.get_embedding.return_value = np.random.randn(64).tolist()

        # A real shared pool, spied so we can count submits.
        shared = ThreadPoolExecutor(max_workers=8)
        try:
            real_submit = shared.submit
            submit_calls = []

            def counting_submit(fn, *args, **kwargs):
                submit_calls.append(fn)
                return real_submit(fn, *args, **kwargs)

            with patch.object(shared, "submit", side_effect=counting_submit):
                # Patch the constructor at its lazy-import source: it must NOT be
                # called on the server path. (search() does
                # `from concurrent.futures import ThreadPoolExecutor`, so the
                # name resolves to concurrent.futures.ThreadPoolExecutor.)
                with patch("concurrent.futures.ThreadPoolExecutor") as ctor_spy:
                    results, timing = store.search(
                        query="test function",
                        embedding_provider=mock_provider,
                        collection_name="test_collection",
                        limit=5,
                        return_timing=True,
                        parallel_executor=shared,
                    )

            # The shared pool ran BOTH parallel tasks (embed + index-load).
            assert len(submit_calls) == 2, (
                f"Expected 2 submits to the shared pool, got {len(submit_calls)}"
            )
            # No fresh per-request executor was constructed on the server path.
            ctor_spy.assert_not_called()
            assert len(results) > 0
            assert timing.get("parallel_execution") is True
        finally:
            shared.shutdown(wait=True)

    def test_injected_executor_is_not_shut_down_by_search(self, tmp_path: Path):
        """The shared pool must survive a search() call (long-lived, reusable)."""
        store = _make_store_with_data(tmp_path)

        mock_provider = Mock()
        mock_provider.get_embedding.return_value = np.random.randn(64).tolist()

        shared = ThreadPoolExecutor(max_workers=4)
        try:
            store.search(
                query="q",
                embedding_provider=mock_provider,
                collection_name="test_collection",
                limit=3,
                parallel_executor=shared,
            )
            # Pool still usable after the call (not shut down).
            fut = shared.submit(lambda: 7)
            assert fut.result(timeout=5) == 7
        finally:
            shared.shutdown(wait=True)

    def test_injected_executor_preserves_exception_propagation(self, tmp_path: Path):
        """A failing embed sub-task must surface the same error via the shared pool."""
        store = _make_store_with_data(tmp_path)

        mock_provider = Mock()
        mock_provider.get_embedding.side_effect = RuntimeError(
            "Embedding API unavailable"
        )

        shared = ThreadPoolExecutor(max_workers=4)
        try:
            with pytest.raises(RuntimeError, match="Embedding API unavailable"):
                store.search(
                    query="q",
                    embedding_provider=mock_provider,
                    collection_name="test_collection",
                    limit=3,
                    parallel_executor=shared,
                )
        finally:
            shared.shutdown(wait=True)


class TestCliPathUnchanged:
    """No injected executor (CLI/solo/daemon): behaviour identical to before."""

    def test_cli_path_constructs_per_call_pool(self, tmp_path: Path):
        """Without parallel_executor, search() still builds ThreadPoolExecutor(max_workers=2)."""
        store = _make_store_with_data(tmp_path)

        mock_provider = Mock()
        mock_provider.get_embedding.return_value = np.random.randn(64).tolist()

        with patch("concurrent.futures.ThreadPoolExecutor") as ctor_spy:
            # Make the patched ctor behave like a real one so search() completes.
            ctor_spy.side_effect = lambda *a, **k: ThreadPoolExecutor(*a, **k)
            store.search(
                query="q",
                embedding_provider=mock_provider,
                collection_name="test_collection",
                limit=3,
            )

        ctor_spy.assert_called_once_with(max_workers=2)

    def test_cli_path_results_match_injected_results(self, tmp_path: Path):
        """Identical results whether or not a shared executor is injected."""
        store = _make_store_with_data(tmp_path, n=20)

        query_vec = np.random.randn(64).tolist()
        provider_cli = Mock()
        provider_cli.get_embedding.return_value = query_vec
        provider_srv = Mock()
        provider_srv.get_embedding.return_value = query_vec

        cli_results, _ = store.search(
            query="same query",
            embedding_provider=provider_cli,
            collection_name="test_collection",
            limit=10,
            return_timing=True,
        )

        shared = ThreadPoolExecutor(max_workers=4)
        try:
            srv_results, _ = store.search(
                query="same query",
                embedding_provider=provider_srv,
                collection_name="test_collection",
                limit=10,
                return_timing=True,
                parallel_executor=shared,
            )
        finally:
            shared.shutdown(wait=True)

        assert len(cli_results) == len(srv_results)
        for a, b in zip(cli_results, srv_results):
            assert a["id"] == b["id"]
            assert abs(a["score"] - b["score"]) < 1e-6
