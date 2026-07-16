"""Story #1400: live submit-side dispatch for async-hybrid temporal queries.

execute_live_temporal_search is the ONE shared entry point BOTH search_code
(MCP) and POST /api/query (REST) call for the temporal branch. It replaces
the old fully-synchronous _execute_temporal_query call with:

    build TemporalWorkerInput (caller's job, not this module's)
    -> compute dedup signature
    -> single-flight join an in-flight identical query via TemporalDedupCache
    -> submit (or join) a BGM lane="temporal" job
    -> foreground-wait, deadline-aware (min(inline_wait, response_deadline))
    -> return the postprocessed inline result (Scenario 1/4) OR a handoff
       envelope (job_id + partial_results + continue_polling=true,
       Scenario 2/3/14)

Real BackgroundJobManager (temporal lane) + real PayloadCache (SQLite tmp
file) + real TemporalDedupCache are used throughout -- only the actual
fusion/embedding work is substituted via a fast fake worker function
(dependency-injected `worker_fn` parameter), matching this project's
anti-mock hierarchy (external/expensive boundaries only).
"""

import time
from typing import Dict, List, Optional

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.maintenance_service import (
    _reset_maintenance_state,
    get_maintenance_state,
)
from code_indexer.server.services.temporal_dedup_cache import TemporalDedupCache
from code_indexer.server.services.temporal_snapshot_store import (
    store_temporal_snapshot,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput


@pytest.fixture(autouse=True)
def _clean_maintenance_state():
    """Guarantee the real, process-wide MaintenanceState singleton is never
    left active across tests, regardless of what other tests in this shared
    services/ pytest process do (or fail to clean up). Without this,
    submit_job() raises MaintenanceModeError whenever an unrelated test
    earlier in the same process entered maintenance mode without exiting
    it -- confirmed root cause of a server-fast-automation.sh chunk-1
    failure of every test in this file."""
    _reset_maintenance_state()
    yield
    get_maintenance_state().exit_maintenance_mode()
    _reset_maintenance_state()


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def bgm(tmp_path):
    from code_indexer.server.utils.config_manager import BackgroundJobsConfig

    manager = BackgroundJobManager(
        storage_path=str(tmp_path / "jobs.json"),
        background_jobs_config=BackgroundJobsConfig(temporal_lane_concurrency=2),
    )
    yield manager
    manager.shutdown()


class _FakeAccessFilteringService:
    """Real-shape stand-in: implements filter_query_results(results, user_id)
    exactly like the real AccessFilteringService, admin bypass included."""

    def __init__(self, admins=None):
        self._admins = admins or {"admin"}

    def is_admin_user(self, user_id):
        return user_id in self._admins

    def filter_query_results(self, results, user_id):
        return results  # this suite's users are all authorized


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


def _make_instant_worker(
    payload_cache: PayloadCache, results: Optional[List[Dict]] = None
):
    """A fast fake worker matching run_temporal_worker's exact call
    contract (worker_input, payload_cache, job_id, progress_callback=None,
    cancel_check=None) -- writes a real, verified terminal snapshot
    immediately via the REAL store_temporal_snapshot, then returns."""

    def _worker(
        worker_input, payload_cache, job_id, progress_callback=None, cancel_check=None
    ):
        store_temporal_snapshot(
            payload_cache,
            job_id,
            {
                "results": results if results is not None else [],
                "shards_completed": 1,
                "shards_total": 1,
                "ctx": {
                    "requested_limit": worker_input.requested_limit,
                    "rerank_query": worker_input.rerank_query,
                    "rerank_instruction": worker_input.rerank_instruction,
                },
            },
            terminal=True,
        )
        return {"result_ready": True}

    return _worker


def _make_slow_worker(payload_cache: PayloadCache, delay_seconds: float):
    """A fake worker that sleeps before writing its terminal snapshot --
    used to deterministically force the async-handoff path (Scenario 2)."""

    def _worker(
        worker_input, payload_cache, job_id, progress_callback=None, cancel_check=None
    ):
        time.sleep(delay_seconds)
        store_temporal_snapshot(
            payload_cache,
            job_id,
            {
                "results": [],
                "shards_completed": 1,
                "shards_total": 1,
                "ctx": {"requested_limit": worker_input.requested_limit},
            },
            terminal=True,
        )
        return {"result_ready": True}

    return _worker


class TestFastCompletionWithinInlineWindow:
    def test_completed_within_budget_returns_completed_status_with_job_id(
        self, tmp_path, payload_cache, bgm
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        worker_input = _make_worker_input(tmp_path)
        result = execute_live_temporal_search(
            worker_input=worker_input,
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=5.0,
            handler_deadline_monotonic=None,
            response_reserve_seconds=1.0,
            dedup_cache=TemporalDedupCache(),
            worker_fn=_make_instant_worker(
                payload_cache, results=[{"file_path": "a.py"}]
            ),
        )

        assert result["status"] == "completed"
        assert result["job_id"]
        assert result["results"] == [{"file_path": "a.py"}]


class TestSlowQueryDegradesToHandoff:
    def test_exceeding_inline_wait_returns_waiting_with_job_id_and_partials(
        self, tmp_path, payload_cache, bgm
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        worker_input = _make_worker_input(tmp_path, query_text="slow query")
        result = execute_live_temporal_search(
            worker_input=worker_input,
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=0.05,
            handler_deadline_monotonic=None,
            response_reserve_seconds=1.0,
            dedup_cache=TemporalDedupCache(),
            worker_fn=_make_slow_worker(payload_cache, delay_seconds=2.0),
        )

        assert result["status"] == "waiting"
        assert result["continue_polling"] is True
        assert result["job_id"]
        assert result["unranked"] is True
        assert "partial_results" in result

    def test_response_deadline_caps_wait_even_with_generous_inline_wait(
        self, tmp_path, payload_cache, bgm
    ):
        """CRITICAL 5: waiter_deadline = min(inline_wait, response_deadline).
        A near response_deadline must cut the wait short even when
        inline_wait_seconds is generous."""
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        worker_input = _make_worker_input(tmp_path, query_text="deadline-capped query")
        near_deadline = time.monotonic() + 0.1
        started = time.monotonic()
        result = execute_live_temporal_search(
            worker_input=worker_input,
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=60.0,  # generous -- must NOT be the binding constraint
            handler_deadline_monotonic=near_deadline
            + 1.0,  # response_reserve makes it ~near_deadline
            response_reserve_seconds=1.0,
            dedup_cache=TemporalDedupCache(),
            worker_fn=_make_slow_worker(payload_cache, delay_seconds=5.0),
        )
        elapsed = time.monotonic() - started

        assert result["status"] == "waiting"
        assert elapsed < 5.0, (
            f"waiter ran for {elapsed:.2f}s -- response_deadline should have "
            "capped it well before inline_wait_seconds=60 or the worker's "
            "own 5s completion delay"
        )


class TestConfigServiceThreading:
    def test_config_service_forwarded_to_poll_temporal_job_status(
        self, tmp_path, payload_cache, bgm
    ):
        from unittest.mock import patch

        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        sentinel_config_service = object()
        captured = {}
        real_poll_status = __import__(
            "code_indexer.server.services.temporal_poll_job_status",
            fromlist=["poll_temporal_job_status"],
        ).poll_temporal_job_status

        def _spy_poll(*args, **kwargs):
            captured.update(kwargs)
            return real_poll_status(*args, **kwargs)

        worker_input = _make_worker_input(tmp_path)
        with patch(
            "code_indexer.server.services.temporal_live_dispatch.poll_temporal_job_status",
            side_effect=_spy_poll,
        ):
            execute_live_temporal_search(
                worker_input=worker_input,
                background_job_manager=bgm,
                payload_cache=payload_cache,
                access_filtering_service=_FakeAccessFilteringService(),
                is_admin=False,
                inline_wait_seconds=5.0,
                handler_deadline_monotonic=None,
                response_reserve_seconds=1.0,
                dedup_cache=TemporalDedupCache(),
                worker_fn=_make_instant_worker(payload_cache),
                config_service=sentinel_config_service,
            )

        assert captured["config_service"] is sentinel_config_service


class TestSingleFlightDedupJoin:
    def test_identical_signature_joins_in_flight_job_no_second_submit(
        self, tmp_path, payload_cache, bgm
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        dedup_cache = TemporalDedupCache()
        worker_input = _make_worker_input(tmp_path, query_text="dedup test query")
        submit_count = {"n": 0}
        slow_worker = _make_slow_worker(payload_cache, delay_seconds=2.0)

        def _counting_worker(*args, **kwargs):
            submit_count["n"] += 1
            return slow_worker(*args, **kwargs)

        kwargs = dict(
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=0.05,
            handler_deadline_monotonic=None,
            response_reserve_seconds=1.0,
            dedup_cache=dedup_cache,
            worker_fn=_counting_worker,
        )

        result1 = execute_live_temporal_search(worker_input=worker_input, **kwargs)
        result2 = execute_live_temporal_search(worker_input=worker_input, **kwargs)

        assert result1["job_id"] == result2["job_id"]
        assert submit_count["n"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
