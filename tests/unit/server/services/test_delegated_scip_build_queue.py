"""Unit tests for the delegated SCIP build queue (EVO-65021, stream-05).

Covers the fail-soft / backpressure / dedupe drain that decouples the (up to
1800s) delegated sandbox build from the golden-repo index job.
"""

from types import SimpleNamespace
from typing import List

import pytest

from code_indexer.server.services.scip_resolution_queue import (
    DelegatedScipBuild,
    DelegatedScipBuildQueue,
    get_delegated_scip_build_queue,
    get_scip_metrics,
    reset_scip_metrics,
)


def _gen_result(*, successful: int = 0, stderrs: List[str] | None = None):
    """Build a GenerationResult-shaped stub the queue understands."""
    project_results = [
        SimpleNamespace(indexer_result=SimpleNamespace(stderr=se))
        for se in (stderrs or [])
    ]
    return SimpleNamespace(
        successful_projects=successful,
        project_results=project_results,
    )


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_scip_metrics()
    yield
    reset_scip_metrics()


def _queue() -> DelegatedScipBuildQueue:
    # Injected no-op sleep keeps backpressure retries instant/deterministic.
    return DelegatedScipBuildQueue(sleep=lambda _s: None)


class TestFailSoft:
    def test_failed_build_is_dropped_not_raised(self):
        """exit-69 unsupported-language → FAILED result → fail-soft drop."""
        q = _queue()
        build = DelegatedScipBuild(
            alias="java-repo",
            dedupe_key="java-repo@abc",
            run_build=lambda: _gen_result(
                successful=0, stderrs=["No SCIP indexer for language (exit 69)"]
            ),
        )
        assert q.enqueue(build, start_worker=False) is True
        # Must not raise, and must fully drain the item.
        assert q.drain_pending() == 1
        assert get_scip_metrics().get("delegated_build_skipped") == 1
        # Dedupe key released so a later commit can rebuild.
        assert q.get_status()["inflight_keys"] == []

    def test_raised_build_is_swallowed(self):
        q = _queue()

        def _boom():
            raise RuntimeError("transport exploded")

        build = DelegatedScipBuild(alias="r", dedupe_key="r@1", run_build=_boom)
        q.enqueue(build, start_worker=False)
        assert q.drain_pending() == 1  # no exception propagates
        assert get_scip_metrics().get("delegated_build_error") == 1


class TestSuccess:
    def test_success_fires_on_success_and_meters(self):
        q = _queue()
        recorded = []
        build = DelegatedScipBuild(
            alias="ts-repo",
            dedupe_key="ts-repo@abc",
            run_build=lambda: _gen_result(successful=1),
            on_success=lambda result: recorded.append(result.successful_projects),
        )
        q.enqueue(build, start_worker=False)
        q.drain_pending()
        assert recorded == [1]
        assert get_scip_metrics().get("delegated_build_success") == 1


class TestBackpressure:
    def test_http_429_reenqueues_then_succeeds(self):
        q = _queue()
        calls = {"n": 0}

        def _run():
            calls["n"] += 1
            if calls["n"] == 1:
                return _gen_result(
                    successful=0, stderrs=["sandbox_scip failed (HTTP 429): busy"]
                )
            return _gen_result(successful=1)

        build = DelegatedScipBuild(alias="ts", dedupe_key="ts@1", run_build=_run)
        q.enqueue(build, start_worker=False)
        # First drain: backpressure → re-enqueued (not terminal). Second: success.
        assert q.drain_pending(max_items=1) == 1
        assert get_scip_metrics().get("delegated_build_backpressure_retry") == 1
        # key retained across the retry
        assert q.get_status()["inflight_keys"] == ["ts@1"]
        assert q.drain_pending(max_items=1) == 1
        assert calls["n"] == 2
        assert get_scip_metrics().get("delegated_build_success") == 1

    def test_backpressure_capped_then_fail_soft(self):
        q = DelegatedScipBuildQueue(sleep=lambda _s: None, max_backpressure_retries=2)
        build = DelegatedScipBuild(
            alias="ts",
            dedupe_key="ts@1",
            run_build=lambda: _gen_result(
                successful=0, stderrs=["HTTP 429 Too Many Requests"]
            ),
        )
        q.enqueue(build, start_worker=False)
        # 2 retries then a terminal fail-soft drop on the 3rd process.
        q.drain_pending(max_items=1)
        q.drain_pending(max_items=1)
        q.drain_pending(max_items=1)
        m = get_scip_metrics()
        assert m.get("delegated_build_backpressure_retry") == 2
        assert m.get("delegated_build_skipped") == 1
        assert q.get_status()["inflight_keys"] == []


class TestDedupe:
    def test_same_key_deduped(self):
        q = _queue()
        b1 = DelegatedScipBuild(
            alias="r", dedupe_key="r@sha", run_build=lambda: _gen_result(successful=1)
        )
        b2 = DelegatedScipBuild(
            alias="r", dedupe_key="r@sha", run_build=lambda: _gen_result(successful=1)
        )
        assert q.enqueue(b1, start_worker=False) is True
        assert q.enqueue(b2, start_worker=False) is False  # deduped
        assert get_scip_metrics().get("delegated_build_deduped") == 1
        assert q.drain_pending() == 1  # only one build ran

    def test_different_commit_not_deduped(self):
        q = _queue()
        b1 = DelegatedScipBuild(
            alias="r", dedupe_key="r@sha1", run_build=lambda: _gen_result(successful=1)
        )
        b2 = DelegatedScipBuild(
            alias="r", dedupe_key="r@sha2", run_build=lambda: _gen_result(successful=1)
        )
        assert q.enqueue(b1, start_worker=False) is True
        assert q.enqueue(b2, start_worker=False) is True
        assert q.drain_pending() == 2


class TestSingleton:
    def test_singleton_is_stable(self):
        assert get_delegated_scip_build_queue() is get_delegated_scip_build_queue()
