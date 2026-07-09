"""
Unit tests for DbOutageThrottle / is_db_connectivity_error (Bug #1249).

Bug #1249: When PostgreSQL briefly restarts/drops, every DB-dependent
background loop (refresh_scheduler, leader_election_service,
node_metrics_writer_service, node_heartbeat_service, config_service) logs a
fresh ERROR + full traceback on EVERY tick with no backoff and no dedup,
flooding logs.db with tens of thousands of rows during a single ~5 minute
outage.

These tests cover the shared classifier + throttle primitive
(`src/code_indexer/server/services/db_outage_throttle.py`) and at least one
real loop integration proving the wiring collapses a per-tick ERROR storm
into a single ERROR + DEBUG follow-ups.

No mocking of psycopg/psycopg_pool exception classes — they are real,
cheap-to-construct exception classes and are instantiated for real here.
"""

import logging

import psycopg
import psycopg_pool

from code_indexer.server.services.db_outage_throttle import (
    DbOutageThrottle,
    is_db_connectivity_error,
)


# ---------------------------------------------------------------------------
# is_db_connectivity_error — pure classifier
# ---------------------------------------------------------------------------


class TestIsDbConnectivityError:
    def test_true_for_operational_error_server_closed_connection(self):
        exc = psycopg.OperationalError("server closed the connection unexpectedly")
        assert is_db_connectivity_error(exc) is True

    def test_true_for_real_pool_timeout_instance(self):
        # PoolTimeout IS-A OperationalError (verified MRO); construct for real.
        exc = psycopg_pool.PoolTimeout("couldn't get a connection within 30.0 sec")
        assert is_db_connectivity_error(exc) is True

    def test_false_for_programming_error(self):
        exc = psycopg.ProgrammingError('syntax error at or near "SELCT"')
        assert is_db_connectivity_error(exc) is False

    def test_false_for_integrity_error(self):
        exc = psycopg.IntegrityError("duplicate key value violates unique constraint")
        assert is_db_connectivity_error(exc) is False

    def test_false_for_unrelated_value_error(self):
        exc = ValueError("totally unrelated")
        assert is_db_connectivity_error(exc) is False

    def test_false_for_unrelated_runtime_error(self):
        exc = RuntimeError("totally unrelated")
        assert is_db_connectivity_error(exc) is False

    def test_true_for_generic_exception_with_connectivity_message(self):
        # Defense-in-depth: a wrapped/stringified signal that lost its type
        # but kept a recognizable connectivity phrase in the message.
        exc = Exception("server terminated abnormally")
        assert is_db_connectivity_error(exc) is True

    def test_true_for_pool_get_connection_message(self):
        exc = Exception("couldn't get a connection from the pool")
        assert is_db_connectivity_error(exc) is True


# ---------------------------------------------------------------------------
# DbOutageThrottle — stateful throttle
# ---------------------------------------------------------------------------


class TestDbOutageThrottleStorm:
    def test_only_first_connectivity_error_logs_at_error_level(self, caplog):
        throttle = DbOutageThrottle(service_name="TestService")
        logger = logging.getLogger("test_db_outage_throttle.storm")

        K = 5
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            for _ in range(K):
                exc = psycopg_pool.PoolTimeout("couldn't get a connection")
                handled = throttle.on_db_error(exc, logger)
                assert handled is True

        error_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno == logging.ERROR
        ]
        debug_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno == logging.DEBUG
        ]

        assert len(error_records) == 1, (
            f"Expected exactly ONE ERROR record across {K} consecutive "
            f"connectivity errors, got {len(error_records)}: "
            f"{[r.message for r in error_records]}"
        )
        assert error_records[0].exc_info is not None

        # The remaining K-1 errors must be logged at DEBUG, not silently dropped.
        assert len(debug_records) == K - 1, (
            f"Expected {K - 1} DEBUG follow-up records, got {len(debug_records)}"
        )

    def test_next_wait_seconds_grows_and_is_capped(self):
        max_backoff = 60.0
        throttle = DbOutageThrottle(
            service_name="TestService", max_backoff_seconds=max_backoff
        )
        logger = logging.getLogger("test_db_outage_throttle.backoff")

        normal_interval = 10.0
        # No outage yet -> unchanged.
        assert throttle.next_wait_seconds(normal_interval) == normal_interval

        waits = []
        for _ in range(50):
            exc = psycopg.OperationalError("server closed the connection unexpectedly")
            throttle.on_db_error(exc, logger)
            waits.append(throttle.next_wait_seconds(normal_interval))

        # Strictly increasing for the first few failures (exponential growth)
        assert waits[1] > waits[0]
        assert waits[2] > waits[1]

        # Bounded: never exceeds max_backoff_seconds even after many failures.
        assert all(w <= max_backoff for w in waits), (
            f"next_wait_seconds exceeded cap {max_backoff}: max observed {max(waits)}"
        )
        # The tail (after many failures) must actually be at the cap, not still
        # growing unboundedly.
        assert waits[-1] == max_backoff

    def test_recovery_logs_once_and_resets_state(self, caplog):
        throttle = DbOutageThrottle(service_name="TestService")
        logger = logging.getLogger("test_db_outage_throttle.recovery")

        with caplog.at_level(logging.DEBUG, logger=logger.name):
            for _ in range(5):
                exc = psycopg_pool.PoolTimeout("couldn't get a connection")
                throttle.on_db_error(exc, logger)

            # No outage->success log noise before recovery is called.
            recovery_records_before = [
                r
                for r in caplog.records
                if r.name == logger.name and r.levelno >= logging.INFO
            ]

            throttle.on_db_success(logger)

        recovery_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno >= logging.INFO
        ]
        # Exactly one recovery-level record (INFO or WARNING) emitted by on_db_success.
        new_recovery_records = [
            r for r in recovery_records if r not in recovery_records_before
        ]
        assert len(new_recovery_records) == 1, (
            f"Expected exactly ONE recovery log record, got {len(new_recovery_records)}: "
            f"{[r.message for r in new_recovery_records]}"
        )

        # State is reset: next_wait_seconds returns the normal interval again.
        assert throttle.next_wait_seconds(10.0) == 10.0

        # A fresh outage cycle must log a fresh ERROR (not permanently suppressed).
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            exc = psycopg_pool.PoolTimeout("couldn't get a connection")
            throttle.on_db_error(exc, logger)

        new_error_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno == logging.ERROR
        ]
        assert len(new_error_records) == 1, (
            "Expected a fresh ERROR record after recovery + new outage, got "
            f"{len(new_error_records)}"
        )

    def test_on_db_success_with_no_outage_in_progress_is_noop(self, caplog):
        throttle = DbOutageThrottle(service_name="TestService")
        logger = logging.getLogger("test_db_outage_throttle.noop_success")

        with caplog.at_level(logging.DEBUG, logger=logger.name):
            throttle.on_db_success(logger)

        records = [r for r in caplog.records if r.name == logger.name]
        assert records == [], (
            f"on_db_success() with no prior outage must not log anything, got: "
            f"{[r.message for r in records]}"
        )

    def test_non_connectivity_error_returns_false_and_never_suppressed(self, caplog):
        throttle = DbOutageThrottle(service_name="TestService")
        logger = logging.getLogger("test_db_outage_throttle.non_connectivity")

        with caplog.at_level(logging.DEBUG, logger=logger.name):
            for _ in range(10):
                exc = ValueError("totally unrelated")
                handled = throttle.on_db_error(exc, logger)
                assert handled is False, (
                    "Non-connectivity errors must never be throttled/suppressed "
                    "(on_db_error must return False every time)"
                )

        # Throttle itself must not have logged anything for non-connectivity errors —
        # the caller is responsible for logging those.
        throttle_records = [r for r in caplog.records if r.name == logger.name]
        assert throttle_records == [], (
            f"DbOutageThrottle must not log for non-connectivity errors, got: "
            f"{[r.message for r in throttle_records]}"
        )

    def test_next_wait_seconds_does_not_overflow_at_extreme_failure_count(self):
        # Bug #1249 follow-up: next_wait_seconds(...) computed
        # `2 ** (count - 1)` as an arbitrary-precision Python int BEFORE the
        # min()-cap ever runs. `_consecutive_failures` is incremented once
        # per connectivity failure in on_db_error and is NEVER itself capped
        # (only the final returned wait value is capped) — so for a
        # long-enough outage `count` grows unbounded and the exponent
        # eventually produces a bignum too large to convert to float,
        # raising OverflowError from OUTSIDE every caller's try/except
        # (next_wait_seconds is always called after the loop's own
        # DB-exception handling), killing the daemon thread this throttle
        # exists to keep alive.
        #
        # Driven entirely through the public on_db_error() API (2000
        # iterations is sub-second — no need to reach into private state).
        max_backoff = 60.0
        throttle = DbOutageThrottle(
            service_name="TestService", max_backoff_seconds=max_backoff
        )
        logger = logging.getLogger("test_db_outage_throttle.overflow_guard")

        for _ in range(2000):
            exc = psycopg.OperationalError("server closed the connection unexpectedly")
            throttle.on_db_error(exc, logger)

        # Must not raise OverflowError, and must be clamped exactly at the cap.
        wait = throttle.next_wait_seconds(10.0)
        assert wait == max_backoff

    def test_next_wait_seconds_does_not_overflow_at_pathological_failure_count(self):
        # Even more extreme: directly push _consecutive_failures to a
        # pathological value (100_000) to prove the bound holds for ANY
        # non-negative integer count, not just what 2000 on_db_error() calls
        # happen to produce. Driving 100_000 real exception constructions
        # through the public API would be wasteful for what is purely a
        # boundary/invariant check on next_wait_seconds' own arithmetic, so
        # this one test reaches directly into the private counter — the
        # other test above already proves the public on_db_error() path
        # reaches this state correctly under sustained real failures.
        max_backoff = 60.0
        throttle = DbOutageThrottle(
            service_name="TestService", max_backoff_seconds=max_backoff
        )
        throttle._consecutive_failures = 100_000

        wait = throttle.next_wait_seconds(10.0)
        assert wait == max_backoff

    def test_throttle_instance_is_independent_per_service(self):
        # Two throttle instances must not share state.
        throttle_a = DbOutageThrottle(service_name="ServiceA")
        throttle_b = DbOutageThrottle(service_name="ServiceB")
        logger = logging.getLogger("test_db_outage_throttle.independence")

        exc = psycopg_pool.PoolTimeout("couldn't get a connection")
        throttle_a.on_db_error(exc, logger)

        # throttle_b has had no errors -> no outage in progress.
        assert throttle_b.next_wait_seconds(10.0) == 10.0
        # throttle_a has an outage in progress -> wait increased (or at least
        # the throttle considers an outage active).
        assert throttle_a.next_wait_seconds(10.0) >= 10.0


# ---------------------------------------------------------------------------
# Real loop integration — proves the wiring collapses the storm end to end.
# ---------------------------------------------------------------------------


class TestNodeHeartbeatServiceLoopIntegration:
    """
    Drives NodeHeartbeatService.update_heartbeat() through repeated
    connectivity failures (via a stub pool whose .connection() raises
    psycopg.OperationalError) and proves only the FIRST failure logs at
    ERROR level — the rest are throttled to DEBUG.
    """

    def _make_failing_pool(self):
        class _FailingConnCtx:
            def __enter__(self):
                raise psycopg.OperationalError(
                    "server closed the connection unexpectedly"
                )

            def __exit__(self, *exc):
                return False

        class _FailingPool:
            def connection(self):
                return _FailingConnCtx()

        return _FailingPool()

    def test_heartbeat_loop_except_block_collapses_storm(self, caplog):
        """
        Directly exercises the _heartbeat_loop()-style except-block wiring by
        calling update_heartbeat() inside the same try/except pattern the
        production loop uses, N times in a row, and asserting only one ERROR
        is logged.
        """
        from code_indexer.server.services.node_heartbeat_service import (
            NodeHeartbeatService,
        )

        service = NodeHeartbeatService(
            pool=self._make_failing_pool(),
            node_id="test-node-1249b",
            heartbeat_interval=10,
        )

        logger = logging.getLogger(
            "code_indexer.server.services.node_heartbeat_service"
        )

        N = 6
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            for _ in range(N):
                try:
                    service.update_heartbeat()
                    service._db_throttle.on_db_success(logger)
                except Exception as exc:
                    if not service._db_throttle.on_db_error(exc, logger):
                        logger.exception(
                            "NodeHeartbeatService [%s]: error updating heartbeat",
                            service.node_id,
                        )

        error_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno == logging.ERROR
        ]
        assert len(error_records) == 1, (
            f"Expected exactly ONE ERROR record across {N} consecutive heartbeat "
            f"connectivity failures after wiring, got {len(error_records)}: "
            f"{[r.message for r in error_records]}"
        )


class TestLeaderElectionServiceLoopIntegration:
    """
    Drives LeaderElectionService.try_acquire_leadership() directly through
    repeated psycopg.connect() connectivity failures and proves only the
    FIRST failure logs at ERROR level.
    """

    def test_try_acquire_leadership_storm_collapses_to_single_error(self, caplog):
        from unittest.mock import patch

        from code_indexer.server.services.leader_election_service import (
            LeaderElectionService,
        )

        service = LeaderElectionService(
            connection_string="postgresql://localhost/test",
            node_id="test-node-1249",
        )

        logger = logging.getLogger(
            "code_indexer.server.services.leader_election_service"
        )

        N = 6
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            with patch(
                "psycopg.connect",
                side_effect=psycopg.OperationalError(
                    "server closed the connection unexpectedly"
                ),
            ):
                for _ in range(N):
                    result = service.try_acquire_leadership()
                    assert result is False

        error_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno == logging.ERROR
        ]
        assert len(error_records) == 1, (
            f"Expected exactly ONE ERROR record across {N} consecutive "
            f"try_acquire_leadership() connectivity failures, got "
            f"{len(error_records)}: {[r.message for r in error_records]}"
        )


class TestNodeMetricsWriterServiceLoopIntegration:
    """
    Drives NodeMetricsWriterService.write_once() directly through repeated
    backend.write_snapshot() connectivity failures and proves only the FIRST
    failure logs at ERROR level.
    """

    def test_write_once_storm_collapses_to_single_error(self, caplog):
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        class _FailingBackend:
            def write_snapshot(self, snapshot):
                raise psycopg.OperationalError(
                    "server closed the connection unexpectedly"
                )

            def cleanup_older_than(self, cutoff):
                return 0

        service = NodeMetricsWriterService(
            backend=_FailingBackend(), node_id="test-node-1249", write_interval=60
        )

        logger = logging.getLogger(
            "code_indexer.server.services.node_metrics_writer_service"
        )

        N = 6
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            for _ in range(N):
                service.write_once()

        error_records = [
            r
            for r in caplog.records
            if r.name == logger.name and r.levelno == logging.ERROR
        ]
        assert len(error_records) == 1, (
            f"Expected exactly ONE ERROR record across {N} consecutive "
            f"write_once() connectivity failures, got {len(error_records)}: "
            f"{[r.message for r in error_records]}"
        )


class TestConfigServiceLoopIntegration:
    """
    Drives ConfigService's real background _poll_loop thread (via
    start_config_reload) through repeated check_config_update() connectivity
    failures and proves only the FIRST failure logs at ERROR level.
    """

    def test_poll_loop_storm_collapses_to_single_error(self, caplog):
        import time as _time
        from unittest.mock import MagicMock

        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService()
        service._pool = MagicMock()  # make start_config_reload proceed
        service.check_config_update = MagicMock(  # type: ignore[method-assign]
            side_effect=psycopg.OperationalError(
                "server closed the connection unexpectedly"
            )
        )
        service.check_pending_launch_restart = MagicMock()  # type: ignore[method-assign]

        logger = logging.getLogger("code_indexer.server.services.config_service")

        try:
            with caplog.at_level(logging.DEBUG, logger=logger.name):
                service.start_config_reload(interval_seconds=0.05)
                # Let the loop tick several times (>5 ticks at 0.05s each).
                _time.sleep(0.4)
        finally:
            service.stop_config_reload()

        error_records = [
            r
            for r in caplog.records
            if r.name == logger.name
            and r.levelno == logging.ERROR
            and "config reload poll failed" in r.message
        ]
        assert len(error_records) <= 1, (
            f"Expected at most ONE ERROR record for the config-reload connectivity "
            f"storm, got {len(error_records)}: {[r.message for r in error_records]}"
        )
        assert service.check_config_update.call_count >= 2, (
            "Test setup invariant: the poll loop must have ticked more than once"
        )


class TestRefreshSchedulerLoopIntegration:
    """
    Drives RefreshScheduler._scheduler_loop() through repeated
    _submit_refresh_job() connectivity failures (one due repo, multiple
    iterations) and proves only the FIRST failure logs at ERROR level for
    the per-repo submit path (Bug #735's outer circuit breaker is untouched
    and not exercised by this test — the whole iteration must succeed except
    for the inner submit call).
    """

    def test_scheduler_loop_submit_storm_collapses_to_single_error(self, caplog):
        from unittest.mock import MagicMock, patch

        from code_indexer.global_repos.cleanup_manager import CleanupManager
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        mock_query_tracker = MagicMock(spec=QueryTracker)
        mock_cleanup_manager = MagicMock(spec=CleanupManager)
        mock_config_source = MagicMock()
        mock_config_source.get_global_refresh_interval.return_value = 3600

        repo = {
            "alias_name": "test-repo-global",
            "repo_url": "git@github.com:test/repo.git",
            "next_refresh": 0.0,
        }
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [repo]
        mock_registry.list_due_repos.return_value = [repo]
        mock_registry.update_next_refresh.return_value = None

        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            scheduler = RefreshScheduler(
                golden_repos_dir=tmp_dir,
                config_source=mock_config_source,
                query_tracker=mock_query_tracker,
                cleanup_manager=mock_cleanup_manager,
                registry=mock_registry,
            )

            logger = logging.getLogger("code_indexer.global_repos.refresh_scheduler")

            N_ITERATIONS = 5
            wait_calls: list = []

            def capturing_wait(timeout=None):
                wait_calls.append(timeout)
                if len(wait_calls) >= N_ITERATIONS:
                    scheduler._running = False
                return False

            scheduler._stop_event.wait = capturing_wait  # type: ignore[method-assign]

            with patch.object(
                scheduler,
                "_submit_refresh_job",
                side_effect=psycopg.OperationalError(
                    "server closed the connection unexpectedly"
                ),
            ):
                with patch.object(scheduler, "cleanup_stale_write_mode_markers"):
                    with caplog.at_level(logging.DEBUG, logger=logger.name):
                        scheduler._running = True
                        scheduler._scheduler_loop()

            error_records = [
                r
                for r in caplog.records
                if r.name == logger.name and r.levelno == logging.ERROR
            ]
            assert len(error_records) == 1, (
                f"Expected exactly ONE ERROR record across {N_ITERATIONS} consecutive "
                f"_submit_refresh_job() connectivity failures, got "
                f"{len(error_records)}: {[r.message for r in error_records]}"
            )
            assert len(wait_calls) >= N_ITERATIONS
