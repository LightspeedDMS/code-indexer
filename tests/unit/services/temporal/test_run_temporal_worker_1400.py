"""
Tests for Story #1400 Phase 8: run_temporal_worker (the actual BGM
temporal-lane worker).

Composes already-built pieces:
- reconstruct_temporal_backend (Phase 3) -- reconstructs config/index_path/
  vector_store from worker_input.repo_path/repository_alias.
- execute_temporal_query_with_fusion (Phase 4) -- with on_shards_discovered/
  on_shard_complete/cancel_check callbacks threaded through.
- convert_temporal_result_to_query_result (Phase 3/8) -- shared conversion.
- store_temporal_snapshot (Phase 7) -- verified writes; job-fatal on final-
  write persistence failure.

CRITICAL 2: the worker declares job_id/progress_callback/cancel_check BY
NAME so BGM's inspect.signature-based injection routes it through the
hard-bound direct-call branch (temporal_lane_concurrency becomes a real
hard bound, not a soft one).

CRITICAL 4 (honest no-auto-resubmit contract) is documented in the
worker's own docstring -- verified by inspection in the final report, no
separate code artifact.

TDD: written BEFORE implementation.
"""

import inspect

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.services.temporal_snapshot_store import (
    TemporalSnapshotPersistenceError,
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import run_temporal_worker
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())
    cache.initialize()
    yield cache
    cache.close()


def _make_worker_input(tmp_path, **overrides) -> TemporalWorkerInput:
    base = dict(
        repo_path=str(tmp_path / "repo"),
        repository_alias="my-repo",
        username="alice",
        query_text="auth logic",
        requested_limit=10,
        fusion_fetch_limit=30,
        time_range=("2024-01-01", "2024-12-31"),
        time_range_raw=None,
        time_range_all=False,
        file_path_filter=None,
        provider_filter=None,
        at_commit=None,
        language=None,
        exclude_language=None,
        exclude_path=None,
        diff_types=None,
        author=None,
        chunk_type=None,
        no_embedding_cache_shortcut=False,
        temporal_embedder=None,
        rerank_query=None,
        rerank_instruction=None,
        min_score_ignored_for_temporal=None,
        file_extensions_ignored_for_temporal=None,
    )
    base.update(overrides)
    return TemporalWorkerInput(**base)


def _make_temporal_result(file_path="a.py", ts=100):
    return TemporalSearchResult(
        file_path=file_path,
        chunk_index=0,
        content="content",
        score=0.9,
        metadata={"commit_hash": "abc"},
        temporal_context={"commit_hash": "abc", "commit_timestamp": ts},
    )


def _patch_backend(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "code_indexer.server.services.temporal_worker.reconstruct_temporal_backend",
        lambda *a, **kw: (
            MagicMock(),
            tmp_path / ".code-indexer" / "index",
            MagicMock(),
        ),
    )


class TestRunTemporalWorkerSignatureContract:
    def test_declares_job_id_progress_callback_cancel_check_by_name(self):
        """CRITICAL 2: BGM's inspect.signature-based injection requires
        these EXACT parameter names to route the worker through the
        hard-bound direct-call branch."""
        sig = inspect.signature(run_temporal_worker)
        assert "job_id" in sig.parameters
        assert "progress_callback" in sig.parameters
        assert "cancel_check" in sig.parameters


class TestRunTemporalWorkerHappyPath:
    def test_writes_final_snapshot_with_converted_results(
        self, payload_cache, tmp_path, monkeypatch
    ):
        worker_input = _make_worker_input(tmp_path)
        final_results = TemporalSearchResults(
            results=[_make_temporal_result("a.py", ts=100)],
            query="auth logic",
            filter_type="time_range",
            filter_value=("2024-01-01", "2024-12-31"),
            total_found=1,
            shards_total=1,
            shards_attempted=1,
            shards_succeeded=1,
        )
        _patch_backend(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "code_indexer.server.services.temporal_worker.execute_temporal_query_with_fusion",
            lambda *a, **kw: final_results,
        )

        result = run_temporal_worker(worker_input, payload_cache, job_id="job-1")

        assert result["result_ready"] is True
        snapshot = read_temporal_snapshot(payload_cache, "job-1")
        assert snapshot is not None
        assert snapshot["terminal"] is True
        assert len(snapshot["results"]) == 1
        assert snapshot["results"][0]["file_path"] == "a.py"
        assert snapshot["shards_total"] == 1

    def test_writes_initial_empty_snapshot_before_fusion_starts(
        self, payload_cache, tmp_path, monkeypatch
    ):
        worker_input = _make_worker_input(tmp_path)
        observed = {}

        def _fake_fusion(*args, **kwargs):
            observed["initial_snapshot"] = read_temporal_snapshot(
                payload_cache, "job-2"
            )
            return TemporalSearchResults(
                results=[],
                query="q",
                filter_type="time_range",
                filter_value=None,
                shards_total=0,
                shards_attempted=0,
                shards_succeeded=0,
            )

        _patch_backend(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "code_indexer.server.services.temporal_worker.execute_temporal_query_with_fusion",
            _fake_fusion,
        )

        run_temporal_worker(worker_input, payload_cache, job_id="job-2")

        assert observed["initial_snapshot"] is not None
        assert observed["initial_snapshot"]["results"] == []
        assert observed["initial_snapshot"]["terminal"] is False


class TestRunTemporalWorkerCallbackWiring:
    def test_fusion_receives_shard_and_cancel_callbacks(
        self, payload_cache, tmp_path, monkeypatch
    ):
        worker_input = _make_worker_input(tmp_path)
        captured_kwargs = {}

        def _fake_fusion(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return TemporalSearchResults(
                results=[],
                query="q",
                filter_type="time_range",
                filter_value=None,
                shards_total=0,
                shards_attempted=0,
                shards_succeeded=0,
            )

        _patch_backend(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "code_indexer.server.services.temporal_worker.execute_temporal_query_with_fusion",
            _fake_fusion,
        )

        run_temporal_worker(
            worker_input, payload_cache, job_id="job-3", cancel_check=lambda: False
        )

        assert callable(captured_kwargs.get("on_shards_discovered"))
        assert callable(captured_kwargs.get("on_shard_complete"))
        assert callable(captured_kwargs.get("cancel_check"))

    def test_final_snapshot_reflects_fusion_return_not_intermediate_checkpoint(
        self, payload_cache, tmp_path, monkeypatch
    ):
        worker_input = _make_worker_input(tmp_path)

        def _fake_fusion(*args, **kwargs):
            kwargs["on_shard_complete"](1, 1, [_make_temporal_result("mid.py", ts=50)])
            return TemporalSearchResults(
                results=[_make_temporal_result("final.py", ts=100)],
                query="q",
                filter_type="time_range",
                filter_value=None,
                shards_total=2,
                shards_attempted=2,
                shards_succeeded=2,
            )

        _patch_backend(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "code_indexer.server.services.temporal_worker.execute_temporal_query_with_fusion",
            _fake_fusion,
        )

        run_temporal_worker(worker_input, payload_cache, job_id="job-4")

        snapshot = read_temporal_snapshot(payload_cache, "job-4")
        assert snapshot["results"][0]["file_path"] == "final.py"
        assert snapshot["shards_total"] == 2


class TestRunTemporalWorkerFinalWriteFailureIsJobFatal:
    def test_final_write_persistence_failure_raises(
        self, payload_cache, tmp_path, monkeypatch
    ):
        worker_input = _make_worker_input(tmp_path)
        final_results = TemporalSearchResults(
            results=[],
            query="q",
            filter_type="time_range",
            filter_value=None,
            shards_total=0,
            shards_attempted=0,
            shards_succeeded=0,
        )
        _patch_backend(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "code_indexer.server.services.temporal_worker.execute_temporal_query_with_fusion",
            lambda *a, **kw: final_results,
        )

        call_count = {"n": 0}
        real_store = __import__(
            "code_indexer.server.services.temporal_snapshot_store",
            fromlist=["store_temporal_snapshot"],
        ).store_temporal_snapshot

        def _flaky_store(cache, job_id, snapshot, terminal):
            call_count["n"] += 1
            if terminal:
                raise TemporalSnapshotPersistenceError("final write failed")
            return real_store(cache, job_id, snapshot, terminal)

        monkeypatch.setattr(
            "code_indexer.server.services.temporal_worker.store_temporal_snapshot",
            _flaky_store,
        )

        with pytest.raises(TemporalSnapshotPersistenceError):
            run_temporal_worker(worker_input, payload_cache, job_id="job-5")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
