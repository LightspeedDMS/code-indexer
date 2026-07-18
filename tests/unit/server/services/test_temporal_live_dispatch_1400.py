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


class TestZeroInlineWaitImmediateHandoffContract:
    """Bug investigation (recurrence of the forced-deferral E2E race in
    test_19_temporal_live_wiring_1400.py): temporal_inline_wait_seconds ==
    0.0 is already a valid, accepted config value (config_manager.py only
    rejects < 0.0), so it deserves a well-defined, race-proof contract:
    "always hand off immediately" -- submit-or-join the job and return the
    deferred envelope WITHOUT ever consulting job status. Since there is no
    status check at all in this mode, there is no race to lose, regardless
    of how fast the underlying job happens to complete (real embedding
    round trip, cache hit, or a job absorbed into an in-flight coalescer
    batch under load)."""

    class _AssertNeverCalledBackgroundJobManager:
        """Deterministic fake -- NOT a mock of the code under test. Fails
        the test loudly if execute_live_temporal_search ever consults job
        status when inline_wait_seconds <= 0.0."""

        def __init__(self, job_id: str) -> None:
            self._job_id = job_id

        def submit_job(self, *args, **kwargs):
            return self._job_id

        def get_job_status(self, job_id, username, is_admin=False):
            raise AssertionError(
                "get_job_status must never be called when "
                "inline_wait_seconds <= 0.0 -- the well-defined immediate-"
                "handoff contract has no status check of any kind"
            )

    def test_zero_inline_wait_returns_waiting_without_any_status_check(
        self, tmp_path, payload_cache
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        worker_input = _make_worker_input(tmp_path, query_text="zero wait query")
        result = execute_live_temporal_search(
            worker_input=worker_input,
            background_job_manager=self._AssertNeverCalledBackgroundJobManager(
                "job-zero"
            ),
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=0.0,
            handler_deadline_monotonic=None,
            response_reserve_seconds=1.0,
            dedup_cache=TemporalDedupCache(),
            worker_fn=lambda *a, **kw: {"result_ready": True},
        )

        assert result["status"] == "waiting"
        assert result["continue_polling"] is True
        assert result["job_id"] == "job-zero"
        assert result["partial_results"] == []


class TestDeadlineStrictlyRespectedNoLateStatusRead:
    """Bug investigation: the OLD loop checked the deadline AFTER a status
    read returned "waiting", then slept the FULL _POLL_INTERVAL_SECONDS
    unconditionally (never capped to the remaining budget) before looping
    back to read status AGAIN -- with no deadline check guarding that
    second read. So a job that transitioned to "completed" during the
    unconditional-sleep overshoot window was reported as "completed" even
    though the wait budget had already been exhausted before that read
    ever happened. The fix checks the deadline BEFORE every status read
    (never reads status once the deadline has passed) and caps each sleep
    to the remaining budget."""

    class _DeadlineRaceBackgroundJobManager:
        """Deterministic fake -- NOT a mock of the code under test. Reports
        "running" for the first `calls_before_completed` get_job_status
        calls, then "completed" from the next call onward. Records the
        number of calls made. Using a CALL-COUNT trigger (rather than real
        worker timing) removes the real-thread-scheduling variance that
        made an earlier real-worker-based version of this test flaky --
        the only remaining source of timing variance is the loop's own
        real-clock behavior, which is what this test actually verifies."""

        def __init__(self, job_id: str, calls_before_completed: int) -> None:
            self._job_id = job_id
            self._calls_before_completed = calls_before_completed
            self.call_count = 0

        def submit_job(self, *args, **kwargs):
            return self._job_id

        def get_job_status(self, job_id, username, is_admin=False):
            self.call_count += 1
            if self.call_count <= self._calls_before_completed:
                return {"status": "running", "error": None}
            return {"status": "completed", "error": None}

    def test_late_completion_after_deadline_is_not_reported_as_completed(
        self, tmp_path, payload_cache
    ):
        """With inline_wait_seconds=0.27 (NOT an exact multiple of the 50ms
        poll interval), the fixed deadline-first/remaining-capped loop
        makes exactly 6 status checks (at t~0/.05/.10/.15/.20/.25, then a
        final capped sleep of ~0.02s brings it to the 0.27s deadline,
        where the loop exits WITHOUT a 7th check). The fake is calibrated
        so ONLY a 7th-or-later call would ever report "completed" -- a
        call that must never happen once the deadline has passed. This
        exact test reliably FAILS against the OLD unconditional-full-
        interval-sleep loop (verified empirically: the old loop's 6th
        sleep overshoots to t~0.30, making an illegitimate 7th call that
        the fake resolves as "completed")."""
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        job_id = "deadline-race-job"
        store_temporal_snapshot(
            payload_cache,
            job_id,
            {
                "results": [{"file_path": "should-not-appear.py"}],
                "shards_completed": 1,
                "shards_total": 1,
                "ctx": {"requested_limit": 10},
            },
            terminal=True,
        )
        fake_bgm = self._DeadlineRaceBackgroundJobManager(
            job_id, calls_before_completed=6
        )

        worker_input = _make_worker_input(tmp_path, query_text="deadline race query")
        result = execute_live_temporal_search(
            worker_input=worker_input,
            background_job_manager=fake_bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=0.27,
            handler_deadline_monotonic=None,
            response_reserve_seconds=1.0,
            dedup_cache=TemporalDedupCache(),
            worker_fn=lambda *a, **kw: {"result_ready": True},
        )

        assert result["status"] == "waiting", (
            "a status read that would report 'completed' must never happen "
            "once the deadline has passed -- the returned status must be "
            f"the last KNOWN 'waiting' result, got: {result}"
        )
        assert fake_bgm.call_count <= 6, (
            "the 7th (would-be 'completed') get_job_status call must never "
            f"happen once the deadline has passed, got {fake_bgm.call_count} calls"
        )


class TestWaiterBudgetObservability:
    """Bug investigation (recurrence of the forced-deferral E2E race in
    test_19_temporal_live_wiring_1400.py): a DEBUG log line recording the
    actual inline_wait_seconds value and the computed waiter budget
    (waiter_deadline - now) received by THIS call, to make a future
    deadline-not-honored regression immediately diagnosable from server
    logs rather than requiring ad hoc instrumentation."""

    def test_logs_inline_wait_seconds_and_waiter_budget(
        self, tmp_path, payload_cache, bgm, caplog
    ):
        import logging

        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        worker_input = _make_worker_input(tmp_path, query_text="observability query")
        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.services.temporal_live_dispatch"
        ):
            execute_live_temporal_search(
                worker_input=worker_input,
                background_job_manager=bgm,
                payload_cache=payload_cache,
                access_filtering_service=_FakeAccessFilteringService(),
                is_admin=False,
                inline_wait_seconds=0.001,
                handler_deadline_monotonic=None,
                response_reserve_seconds=1.0,
                dedup_cache=TemporalDedupCache(),
                worker_fn=_make_instant_worker(payload_cache),
            )

        matching = [
            r
            for r in caplog.records
            if "waiter_budget" in r.getMessage()
            and "inline_wait_seconds=0.001000" in r.getMessage()
        ]
        assert matching, (
            "expected a DEBUG log line recording inline_wait_seconds=0.001000 "
            f"and waiter_budget; got records: {[r.getMessage() for r in caplog.records]}"
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
