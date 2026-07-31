"""Unit tests for the delegated SCIP build queue (EVO-65021, stream-05).

Covers the fail-soft / backpressure / dedupe drain that decouples the (up to
1800s) delegated sandbox build from the golden-repo index job.
"""

import threading
import time
from types import SimpleNamespace
from typing import List, Optional

import pytest

from code_indexer.scip.generator import GenerationResult
from code_indexer.server.services.scip_resolution_queue import (
    DELEGATED_BACKPRESSURE_RETRY_SECONDS,
    DELEGATED_MAX_BACKPRESSURE_RETRIES,
    DelegatedScipBuild,
    DelegatedScipBuildQueue,
    get_delegated_scip_build_queue,
    get_scip_metrics,
    reset_scip_metrics,
)


def _gen_result(
    *,
    successful: int = 0,
    failed: int = 0,
    total: Optional[int] = None,
    unsupported: int = 0,
    stderrs: List[str] | None = None,
):
    """Build a real ``GenerationResult`` so the queue exercises its real predicates.

    EVO-65021: ``_result_is_success`` now calls
    ``GenerationResult.is_fresh_eligible()`` (``total>0`` and no REAL failure,
    where a real failure is ``failed - unsupported_language_projects``). The stub
    therefore carries ``total_projects``/``failed_projects`` and, for the
    freshness design, ``unsupported`` (benign exit-69 unsupported-language
    skips). ``project_results`` are lightweight namespaces the backpressure
    scanner reads via ``getattr``.
    """
    project_results = [
        SimpleNamespace(indexer_result=SimpleNamespace(stderr=se))
        for se in (stderrs or [])
    ]
    if total is None:
        total = successful + failed
    return GenerationResult(
        total_projects=total,
        successful_projects=successful,
        failed_projects=failed,
        project_results=project_results,
        unsupported_language_projects=unsupported,
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


class TestPartialSuccessNotTerminalSuccess:
    """MAJOR-3: a partially-successful build must NOT count as full success."""

    def test_partial_success_is_not_marked_success(self):
        q = _queue()
        recorded: List[object] = []
        build = DelegatedScipBuild(
            alias="mixed",
            dedupe_key="mixed@1",
            # 1 TS project ok, 1 non-TS project exit-69 → partial success.
            run_build=lambda: _gen_result(
                successful=1, failed=1, stderrs=["ts ok", "python exit 69"]
            ),
            on_success=lambda r: recorded.append(r),
        )
        q.enqueue(build, start_worker=False)
        assert q.drain_pending() == 1
        m = get_scip_metrics()
        # Not a build success: on_success (freshness marker) never fires, so the
        # failed language is retried at the next build rather than marked fresh.
        assert m.get("delegated_build_success") is None
        assert recorded == []
        # No 429 marker → terminal fail-soft drop, dedupe key released.
        assert m.get("delegated_build_skipped") == 1
        assert q.get_status()["inflight_keys"] == []

    def test_unsupported_language_only_partial_is_marked_fresh(self):
        """Freshness design: a mixed repo whose ONLY failures are unsupported
        languages (exit 69) IS fresh-eligible → on_success fires (marker written),
        so it does not thrash. Distinct from a REAL partial failure above."""
        q = _queue()
        recorded: List[object] = []
        build = DelegatedScipBuild(
            alias="mixed",
            dedupe_key="mixed@1",
            # 1 TS project ok, 1 non-TS project exit-69 (unsupported) → all
            # failures are benign unsupported-language skips.
            run_build=lambda: _gen_result(successful=1, failed=1, unsupported=1),
            on_success=lambda r: recorded.append(r.successful_projects),
        )
        q.enqueue(build, start_worker=False)
        assert q.drain_pending() == 1
        m = get_scip_metrics()
        assert m.get("delegated_build_success") == 1
        assert recorded == [1]  # on_success fired with the produced-index count
        assert m.get("delegated_build_skipped") is None
        assert q.get_status()["inflight_keys"] == []

    def test_pure_unsupported_repo_is_marked_fresh(self):
        """A repo with NO supported project (all exit-69, zero produced indexes)
        is still fresh-eligible so it does not re-enqueue forever."""
        q = _queue()
        recorded: List[object] = []
        build = DelegatedScipBuild(
            alias="pure-java",
            dedupe_key="pure-java@1",
            run_build=lambda: _gen_result(successful=0, failed=2, unsupported=2),
            on_success=lambda r: recorded.append(r.successful_projects),
        )
        q.enqueue(build, start_worker=False)
        assert q.drain_pending() == 1
        assert get_scip_metrics().get("delegated_build_success") == 1
        assert recorded == [0]  # produced no index → on_success sees 0

    def test_real_failure_alongside_unsupported_is_not_fresh(self):
        """A genuine build failure next to unsupported-language skips is NOT
        fresh-eligible (MAJOR-3 partial-masking preserved for real failures)."""
        q = _queue()
        recorded: List[object] = []
        build = DelegatedScipBuild(
            alias="mixed",
            dedupe_key="mixed@1",
            # 3 failed: 2 unsupported (benign) + 1 real toolchain failure.
            run_build=lambda: _gen_result(successful=1, failed=3, unsupported=2),
            on_success=lambda r: recorded.append(r),
        )
        q.enqueue(build, start_worker=False)
        assert q.drain_pending() == 1
        assert get_scip_metrics().get("delegated_build_success") is None
        assert recorded == []
        assert get_scip_metrics().get("delegated_build_skipped") == 1

    def test_partial_success_with_429_is_retried_not_marked_fresh(self):
        q = _queue()
        build = DelegatedScipBuild(
            alias="mixed",
            dedupe_key="mixed@1",
            run_build=lambda: _gen_result(
                successful=1, failed=1, stderrs=["ts ok", "sandbox (HTTP 429) busy"]
            ),
        )
        q.enqueue(build, start_worker=False)
        # Partial + 429 must be retried (was silently treated as success before).
        assert q.drain_pending(max_items=1) == 1
        assert get_scip_metrics().get("delegated_build_backpressure_retry") == 1
        assert q.get_status()["inflight_keys"] == ["mixed@1"]


class TestBackpressureBackoffAndDefaults:
    """MAJOR-4 / Finding-6: the backoff value and shipped defaults are asserted."""

    def test_sleep_called_with_configured_backoff(self):
        slept: List[float] = []
        q = DelegatedScipBuildQueue(sleep=lambda s: slept.append(s))
        build = DelegatedScipBuild(
            alias="ts",
            dedupe_key="ts@1",
            run_build=lambda: _gen_result(successful=0, failed=1, stderrs=["HTTP 429"]),
        )
        q.enqueue(build, start_worker=False)
        q.drain_pending(max_items=1)
        # Deleting the sleep call would now break this test.
        assert slept == [DELEGATED_BACKPRESSURE_RETRY_SECONDS]

    def test_shipped_constant_defaults(self):
        # A regression in either constant is now visible.
        assert DELEGATED_BACKPRESSURE_RETRY_SECONDS == 30.0
        assert DELEGATED_MAX_BACKPRESSURE_RETRIES == 5
        q = DelegatedScipBuildQueue()
        assert q._backpressure_retry_seconds == 30.0
        assert q._max_backpressure_retries == 5


class TestDedupeKeyReleaseAndHooks:
    """MAJOR-4 / Finding-6: key release on the raise path; on_success is guarded."""

    def test_dedupe_key_released_on_raised_build(self):
        q = _queue()

        def _boom():
            raise RuntimeError("transport exploded")

        build = DelegatedScipBuild(alias="r", dedupe_key="r@1", run_build=_boom)
        q.enqueue(build, start_worker=False)
        q.drain_pending()
        # The key must not stick in flight, or the repo could never retry.
        assert q.get_status()["inflight_keys"] == []

    def test_on_success_raising_is_swallowed(self):
        q = _queue()

        def _boom(_r):
            raise RuntimeError("freshness marker write failed")

        build = DelegatedScipBuild(
            alias="r",
            dedupe_key="r@1",
            run_build=lambda: _gen_result(successful=1),
            on_success=_boom,
        )
        q.enqueue(build, start_worker=False)
        assert q.drain_pending() == 1  # best-effort hook, no propagation
        assert get_scip_metrics().get("delegated_build_success") == 1
        assert q.get_status()["inflight_keys"] == []

    def test_key_released_when_sleep_raises_on_backpressure(self):
        # MAJOR-4: a raise from _sleep on the backpressure branch must still
        # release the dedupe key (it is released in a finally on every non-requeue
        # exit path).
        def _explode(_s):
            raise RuntimeError("sleep exploded")

        q = DelegatedScipBuildQueue(sleep=_explode)
        build = DelegatedScipBuild(
            alias="a",
            dedupe_key="a@1",
            run_build=lambda: _gen_result(successful=0, failed=1, stderrs=["HTTP 429"]),
        )
        q.enqueue(build, start_worker=False)
        with pytest.raises(RuntimeError):
            q.drain_pending(max_items=1)  # drain has no guard; the worker loop does
        assert q.get_status()["inflight_keys"] == []


class TestProductionWorkerPath:
    """Finding-6 (1): the REAL daemon-thread ``_worker_loop`` drains, not the seam."""

    def _wait_until(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        assert predicate(), "condition not met within timeout"

    def test_real_worker_thread_drains_and_releases(self):
        q = _queue()
        done = threading.Event()
        build = DelegatedScipBuild(
            alias="ts",
            dedupe_key="ts@1",
            run_build=lambda: _gen_result(successful=1),
            on_success=lambda _r: done.set(),
        )
        # start_worker defaults to True → spawns the real daemon thread.
        assert q.enqueue(build) is True
        assert done.wait(timeout=5.0)
        self._wait_until(lambda: q.get_status()["inflight_keys"] == [])
        assert get_scip_metrics().get("delegated_build_success") == 1
        q.stop()

    def test_worker_loop_survives_unhandled_error_and_keeps_draining(self):
        # A sleep that raises turns the backpressure branch into an unhandled
        # error inside _run_and_maybe_requeue. The singleton worker (no
        # supervisor) MUST survive it and keep draining subsequent builds
        # (MAJOR-4).
        def _explode(_s):
            raise RuntimeError("sleep exploded")

        q = DelegatedScipBuildQueue(sleep=_explode)
        second_done = threading.Event()
        b1 = DelegatedScipBuild(
            alias="a",
            dedupe_key="a@1",
            run_build=lambda: _gen_result(successful=0, failed=1, stderrs=["HTTP 429"]),
        )
        b2 = DelegatedScipBuild(
            alias="b",
            dedupe_key="b@1",
            run_build=lambda: _gen_result(successful=1),
            on_success=lambda _r: second_done.set(),
        )
        q.enqueue(b1)
        q.enqueue(b2)
        # b2 only runs if the worker survived b1's raise.
        assert second_done.wait(timeout=5.0)
        m = get_scip_metrics()
        assert m.get("delegated_worker_loop_error", 0) >= 1
        assert m.get("delegated_build_success") == 1
        self._wait_until(lambda: "a@1" not in q.get_status()["inflight_keys"])
        q.stop()


class TestSingleton:
    def test_singleton_is_stable(self):
        assert get_delegated_scip_build_queue() is get_delegated_scip_build_queue()
