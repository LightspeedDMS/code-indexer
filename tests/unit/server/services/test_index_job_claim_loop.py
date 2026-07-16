"""IndexJobClaimLoop claim -> dispatch -> complete/fail behavior."""

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


class TestProcessOneJob:
    def test_none_claim_is_noop(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = None
        loop = _loop(claimer, {"add_golden_repo": lambda md: {"ok": True}})
        assert loop._process_one_job() is False
        claimer.complete_job.assert_not_called()
        claimer.fail_job.assert_not_called()

    def test_claim_dispatch_complete(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job(metadata={"alias": "a"})
        seen = {}

        def executor(md):
            seen.update(md)
            return {"success": True}

        loop = _loop(claimer, {"add_golden_repo": executor})
        assert loop._process_one_job() is True
        assert seen == {"alias": "a"}
        claimer.complete_job.assert_called_once_with("j1", {"success": True})
        claimer.fail_job.assert_not_called()

    def test_executor_exception_fails_job(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = _job()

        def boom(md):
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
        loop = _loop(claimer, {"add_golden_repo": lambda md: None})
        assert loop._process_one_job() is True
        claimer.fail_job.assert_called_once()

    def test_claim_restricted_to_dispatch_keys(self):
        claimer = MagicMock()
        claimer.claim_next_job.return_value = None
        loop = _loop(
            claimer,
            {"add_golden_repo": lambda md: None, "sync_repository": lambda md: None},
        )
        loop._process_one_job()
        kwargs = claimer.claim_next_job.call_args.kwargs
        assert kwargs["job_types"] == ["add_golden_repo", "sync_repository"]


class TestStopReleasesInflight:
    def test_stop_releases_in_flight_claim(self):
        claimer = MagicMock()
        loop = _loop(claimer, {"add_golden_repo": lambda md: None})
        loop._in_flight = "j-running"
        loop.stop()
        claimer.release_job.assert_called_once_with("j-running")

    def test_stop_no_inflight_no_release(self):
        claimer = MagicMock()
        loop = _loop(claimer, {"add_golden_repo": lambda md: None})
        loop.stop()
        claimer.release_job.assert_not_called()


class TestStartGuards:
    def test_empty_dispatch_does_not_start(self):
        claimer = MagicMock()
        loop = _loop(claimer, {})
        loop.start()
        assert loop._thread is None
