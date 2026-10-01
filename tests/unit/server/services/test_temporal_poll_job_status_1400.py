"""
Tests for Story #1400 Phase 8: poll_temporal_job_status (DEFERRED POLL
algorithm core logic).

Shared by the (not-yet-registered) poll_search_job MCP tool and REST
GET /api/query/result/{job_id} -- both are thin readers around this pure
function, ownership already checked by the caller's get_job_status() call
(this function receives the ALREADY-authorized job_status dict, or None
for not-found/unauthorized -- get_job_status returns None for both cases
by design, so this function cannot distinguish them, matching the locked
algorithm exactly).

    status_of(job_id,user): d=get_job_status(job_id,user); RETURN d["status"] if d else None
    IF st is None: RETURN {status:"not_found", continue_polling:False}
    IF st in ("pending","running"):
      IF has_key: partial (unranked=True always)
      ELSE:       empty partial (Scenario 14)
    IF st in ("failed","cancelled"): RETURN {status:"failed", ...}
    IF NOT has_key: RETURN {status:"not_found", error:"result expired -- resubmit"}  # AC10
    RETURN {status:"completed", results:..., unranked:...}

TDD: written BEFORE implementation.
"""

import pytest

from code_indexer.server.services.temporal_poll_job_status import (
    poll_temporal_job_status,
)


class _FakeAccessFilteringService:
    def is_admin_user(self, user_id):
        return False

    def filter_query_results(self, results, user_id):
        return results


def _snapshot(results=None):
    return {
        "results": results or [],
        "shards_completed": 1,
        "shards_total": 2,
        "ctx": {"requested_limit": 10},
    }


class TestJobNotFound:
    def test_none_job_status_returns_not_found(self):
        result = poll_temporal_job_status(
            job_status=None,
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "not_found"
        assert result["continue_polling"] is False


class TestPendingRunning:
    def test_waiting_with_partial_results_when_snapshot_present(self):
        result = poll_temporal_job_status(
            job_status={"status": "running"},
            read_snapshot_fn=lambda: _snapshot([{"file_path": "a.py"}]),
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "waiting"
        assert result["continue_polling"] is True
        assert result["unranked"] is True
        assert len(result["partial_results"]) == 1
        assert result["shards_completed"] == 1
        assert result["shards_total"] == 2

    def test_empty_partial_when_no_snapshot_yet(self):
        """Scenario 14: zero-shard/PENDING at deadline -- no error for a
        missing snapshot key, just an empty partial."""
        result = poll_temporal_job_status(
            job_status={"status": "pending"},
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "waiting"
        assert result["partial_results"] == []
        assert result["shards_total"] is None
        assert result["unranked"] is True


class TestFailedCancelled:
    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    def test_failed_or_cancelled_reports_failed(self, status):
        result = poll_temporal_job_status(
            job_status={"status": status, "error": "boom"},
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "failed"
        assert result["continue_polling"] is False
        assert result["error"] == "boom"


class TestFailedStatusErrorClassification:
    """Code review finding: CRITICAL 4 requires resubmit_required=True +
    error_code=TEMPORAL_NODE_RESTART for a node-restart-orphaned job;
    CRITICAL 6 requires TEMPORAL_SNAPSHOT_PERSISTENCE_FAILED to be
    machine-readable (distinguishable from plain TTL expiry, which stays
    status=not_found via the separate branch below)."""

    def test_node_restart_orphaned_job_carries_resubmit_required_and_error_code(
        self,
    ):
        result = poll_temporal_job_status(
            job_status={
                "status": "failed",
                "error": "Job interrupted by server restart",
            },
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "failed"
        assert result["continue_polling"] is False
        assert result["resubmit_required"] is True
        assert result["error_code"] == "TEMPORAL_NODE_RESTART"

    def test_snapshot_persistence_failure_carries_distinct_error_code(self):
        result = poll_temporal_job_status(
            job_status={
                "status": "failed",
                "error": (
                    "Temporal snapshot write verification failed for job "
                    "'job-1': expected write_id='abc', got=None"
                ),
            },
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "failed"
        assert result["resubmit_required"] is True
        assert result["error_code"] == "TEMPORAL_SNAPSHOT_PERSISTENCE_FAILED"

    def test_generic_failure_has_no_error_code(self):
        """A genuine query/fusion failure unrelated to node-restart or
        snapshot persistence must NOT be misclassified with either
        machine-readable error_code."""
        result = poll_temporal_job_status(
            job_status={"status": "failed", "error": "fusion query error"},
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "failed"
        assert "error_code" not in result
        assert "resubmit_required" not in result


class TestCompletedWithSnapshot:
    def test_completed_returns_postprocessed_results(self):
        result = poll_temporal_job_status(
            job_status={"status": "completed"},
            read_snapshot_fn=lambda: _snapshot([{"file_path": "a.py"}]),
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "completed"
        assert result["continue_polling"] is False
        assert len(result["results"]) == 1

    def test_completed_includes_total_results_matching_len_results(self):
        """Bug #1434: the completed-poll response omitted total_results
        even though the inline (non-deferred) temporal REST response
        includes it alongside results[]. A realistic multi-result snapshot
        must yield total_results == len(results)."""
        multi_results = [
            {"file_path": "a.py"},
            {"file_path": "b.py"},
            {"file_path": "c.py"},
        ]
        result = poll_temporal_job_status(
            job_status={"status": "completed"},
            read_snapshot_fn=lambda: _snapshot(multi_results),
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "completed"
        assert "total_results" in result
        assert result["total_results"] == len(result["results"])
        assert result["total_results"] == 3

    def test_completed_total_results_matches_inline_response_semantics(self):
        """Regression (Bug #1434's own suggested test): total_results must
        carry IDENTICAL semantics between the inline completed REST
        response (routers/inline_query.py: total_results=len(results)) and
        this polled completed response, for the same underlying result
        set."""
        multi_results = [{"file_path": "a.py"}, {"file_path": "b.py"}]
        result = poll_temporal_job_status(
            job_status={"status": "completed"},
            read_snapshot_fn=lambda: _snapshot(multi_results),
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        # Mirrors the inline path's own computation exactly:
        # `total_results: len(results)` in inline_query.py.
        inline_style_total_results = len(result["results"])
        assert result["total_results"] == inline_style_total_results


class TestCompletedWithoutSnapshotIsExpiry:
    def test_completed_but_missing_snapshot_is_expired_not_found(self):
        """AC10: a COMPLETED job whose snapshot expired past TTL must
        report not_found/resubmit, never stale-empty-as-success."""
        result = poll_temporal_job_status(
            job_status={"status": "completed"},
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "not_found"
        assert result["continue_polling"] is False
        assert "expired" in result["error"].lower()

    def test_completed_with_explicit_none_result_is_still_expired_not_found(self):
        """Bug #1499 regression guard: a genuine temporal job's job_status
        dict always carries a "result" key (BackgroundJobManager.get_job_status
        always includes it, defaulting to None for a temporal job -- only
        the PayloadCache-backed snapshot is the real result store for
        temporal jobs). This must NOT be misinterpreted as "has a real
        persisted result" -- the original TTL-expiry contract must survive
        the Bug #1499 fix unchanged."""
        result = poll_temporal_job_status(
            job_status={"status": "completed", "result": None},
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "not_found"
        assert result["continue_polling"] is False
        assert "expired" in result["error"].lower()

    def test_completed_temporal_job_with_result_ready_sentinel_and_expired_snapshot_is_expired_not_found(
        self,
    ):
        """Regression guard (reviewer finding on the #1499 fix): a REAL
        temporal search job's job_status["result"] is NOT None once the
        worker completes -- run_temporal_worker (temporal_worker.py) returns
        {"result_ready": True}, which background_jobs.py persists verbatim
        as job.result, and get_job_status surfaces it as-is. That value IS
        a dict, so a bare `isinstance(job_status["result"], dict)` check
        (the original, buggy #1499 discriminator) would incorrectly treat
        this as "a genuine non-temporal persisted result" and return a
        bogus completed-with-no-actual-results response instead of the
        honest AC10 expiry. operation_type="temporal_query" is the exact
        string temporal_live_dispatch.py's TEMPORAL_OPERATION_TYPE submits
        every live temporal search job under -- this must route through
        the snapshot/AC10-expiry branch, never the persisted-result
        passthrough meant only for non-temporal jobs like X-Ray."""
        result = poll_temporal_job_status(
            job_status={
                "status": "completed",
                "operation_type": "temporal_query",
                "result": {"result_ready": True},
            },
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "not_found"
        assert result["continue_polling"] is False
        assert "expired" in result["error"].lower()


class TestCompletedNonTemporalJobWithPersistedResult:
    """Bug #1499: a non-temporal completed job (e.g. operation_type=
    xray_search) polled through this SAME deferred-poll path (poll_search_job
    MCP tool / GET /api/query/result/{job_id}) never had a temporal snapshot
    written for it -- read_snapshot_fn() correctly returns None. The real
    result lives in background_jobs.result (job_status["result"]), and must
    be returned as-is instead of the misleading "result expired -- resubmit"
    error."""

    def test_non_temporal_completed_job_returns_persisted_result(self):
        xray_result = {
            "success": True,
            "matches": [{"file": "a.py", "line": 10}],
            "evaluation_errors": [],
        }
        result = poll_temporal_job_status(
            job_status={
                "status": "completed",
                "operation_type": "xray_search",
                "result": xray_result,
            },
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["status"] == "completed"
        assert result["continue_polling"] is False
        assert "error" not in result

    def test_non_temporal_completed_job_result_passes_through_unmodified(self):
        """The result shape (including a cache_handle, which a temporal
        snapshot response never carries) must reach the caller byte-for-byte
        -- never routed through postprocess_temporal_snapshot, whose
        result-shape assumptions (results/shards_completed/shards_total)
        do not apply to an X-Ray result."""
        xray_result = {
            "success": True,
            "matches": [{"file": "a.py", "line": 10}],
            "evaluation_errors": ["timeout on shard 3"],
            "cache_handle": "handle-abc123",
        }
        result = poll_temporal_job_status(
            job_status={
                "status": "completed",
                "operation_type": "xray_search",
                "result": xray_result,
            },
            read_snapshot_fn=lambda: None,
            access_filtering_service=_FakeAccessFilteringService(),
            username="alice",
            is_admin=False,
        )
        assert result["matches"] == xray_result["matches"]
        assert result["evaluation_errors"] == xray_result["evaluation_errors"]
        assert result["cache_handle"] == "handle-abc123"
        assert result["success"] is True
        # Polling-contract fields layered on top, never reshaped by
        # postprocess_temporal_snapshot's results/shards_* shape.
        assert result["status"] == "completed"
        assert result["continue_polling"] is False
        assert "results" not in result
        assert "shards_completed" not in result
        assert "shards_total" not in result
        assert "unranked" not in result

    def test_non_temporal_completed_job_never_calls_postprocess(self):
        """Discriminating assertion: postprocess_temporal_snapshot must not
        even be invoked for this path -- proves the result is returned
        as-is, not merely coincidentally shaped the same."""
        from unittest.mock import patch

        xray_result = {"success": True, "matches": []}
        with patch(
            "code_indexer.server.services.temporal_poll_job_status.postprocess_temporal_snapshot"
        ) as mock_postprocess:
            poll_temporal_job_status(
                job_status={
                    "status": "completed",
                    "operation_type": "xray_search",
                    "result": xray_result,
                },
                read_snapshot_fn=lambda: None,
                access_filtering_service=_FakeAccessFilteringService(),
                username="alice",
                is_admin=False,
            )
        mock_postprocess.assert_not_called()


class TestConfigServiceThreading:
    """Story #1400: config_service/deadline_monotonic must reach
    postprocess_temporal_snapshot on a completed (terminal) read so the
    real rerank wiring (already proven at the postprocessor's own test
    level) is actually reachable end-to-end, not merely dead capability."""

    def test_completed_read_forwards_config_service_and_deadline(self):
        from unittest.mock import patch

        sentinel_config_service = object()
        sentinel_deadline = 123.45
        captured = {}

        def _fake_postprocess(
            snapshot, access_svc, username, is_admin, terminal, **kwargs
        ):
            captured.update(kwargs)
            return [], 1, 2, True

        with patch(
            "code_indexer.server.services.temporal_poll_job_status.postprocess_temporal_snapshot",
            side_effect=_fake_postprocess,
        ):
            poll_temporal_job_status(
                job_status={"status": "completed"},
                read_snapshot_fn=lambda: _snapshot([{"file_path": "a.py"}]),
                access_filtering_service=_FakeAccessFilteringService(),
                username="alice",
                is_admin=False,
                config_service=sentinel_config_service,
                deadline_monotonic=sentinel_deadline,
            )

        assert captured["config_service"] is sentinel_config_service
        assert captured["deadline_monotonic"] == sentinel_deadline


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
