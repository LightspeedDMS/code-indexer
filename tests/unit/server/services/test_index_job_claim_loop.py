"""IndexJobClaimLoop claim -> dispatch -> complete/fail behavior.

Dispatch executors take (metadata, progress_callback): PR #1424 H2 gives a
work-stolen job a real DB-backed progress path via a per-job progress_callback
that routes to DistributedJobClaimer.update_progress.
"""

from unittest.mock import MagicMock

from code_indexer.server.services.index_job_claim_loop import IndexJobClaimLoop


def _loop(claimer, dispatch):
    return IndexJobClaimLoop(claimer=claimer, dispatch=dispatch, node_id="n1")


def _job(op="add_golden_repo", metadata=None):
    return {
        "job_id": "j1",
        "operation_type": op,
        "metadata": metadata if metadata is not None else {"alias": "a"},
    }


class TestClaimAndDispatch:
    def test_none_claim_is_noop(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = None
        loop = _loop(claimer, {"add_golden_repo": lambda md, pc: {"ok": True}})
        assert loop._process_one_job() is False
        claimer.complete_job.assert_not_called()
        claimer.fail_job.assert_not_called()

    def test_claim_dispatch_complete(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job(metadata={"alias": "a"})
        seen = {}

        def executor(md, progress_callback):
            seen.update(md)
            return {"success": True}

        loop = _loop(claimer, {"add_golden_repo": executor})
        assert loop._process_one_job() is True
        assert seen == {"alias": "a"}
        claimer.complete_job.assert_called_once_with("j1", {"success": True})
        claimer.fail_job.assert_not_called()

    def test_claim_restricted_to_dispatch_keys(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = None
        loop = _loop(
            claimer,
            {
                "add_golden_repo": lambda md, pc: None,
                "sync_repository": lambda md, pc: None,
            },
        )
        loop._process_one_job()
        kwargs = claimer.claim_next_job.call_args.kwargs
        assert kwargs["job_types"] == ["add_golden_repo", "sync_repository"]


class TestProgressCallback:
    def test_executor_receives_progress_callback_routing_to_claimer(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job()

        def executor(md, progress_callback):
            # The executor drives progress; it must land on the shared row via
            # the claimer, scoped to THIS job_id.
            progress_callback(60, phase="index", detail="building")
            return {"success": True}

        loop = _loop(claimer, {"add_golden_repo": executor})
        assert loop._process_one_job() is True
        claimer.update_progress.assert_called_once_with(
            "j1", 60, phase="index", detail="building"
        )

    def test_progress_callback_tolerates_positional_only(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job()

        def executor(md, progress_callback):
            # _execute_repository_sync calls progress_callback(int) positionally.
            progress_callback(25)
            return {"success": True}

        loop = _loop(claimer, {"add_golden_repo": executor})
        assert loop._process_one_job() is True
        claimer.update_progress.assert_called_once_with(
            "j1", 25, phase=None, detail=None
        )


class TestFailurePaths:
    def test_executor_exception_fails_job(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job()

        def boom(md, pc):
            raise RuntimeError("kaboom")

        loop = _loop(claimer, {"add_golden_repo": boom})
        assert loop._process_one_job() is True
        claimer.complete_job.assert_not_called()
        args = claimer.fail_job.call_args[0]
        assert args[0] == "j1"
        assert "kaboom" in args[1]

    def test_unknown_op_is_failed_not_stranded(self):
        # Claim filtered to dispatch keys, but defend anyway.
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job(op="mystery")
        loop = _loop(claimer, {"add_golden_repo": lambda md, pc: None})
        assert loop._process_one_job() is True
        claimer.fail_job.assert_called_once()


class TestStopReleasesInflight:
    def test_stop_releases_in_flight_claim(self):
        claimer = MagicMock()
        loop = _loop(claimer, {"add_golden_repo": lambda md, pc: None})
        loop._in_flight = "j-running"
        loop.stop()
        claimer.release_job.assert_called_once_with("j-running")

    def test_stop_no_inflight_no_release(self):
        claimer = MagicMock()
        loop = _loop(claimer, {"add_golden_repo": lambda md, pc: None})
        loop.stop()
        claimer.release_job.assert_not_called()


class TestStartGuards:
    def test_empty_dispatch_does_not_start(self):
        claimer = MagicMock()
        loop = _loop(claimer, {})
        loop.start()
        assert loop._thread is None
