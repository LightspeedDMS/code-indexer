"""Unit tests for Story #1418 Phase 3 Component 9:
EmbeddingStatsRetentionScheduler.

Mirrors ActivatedReaperScheduler's exact simple template (Story #967):
daemon-thread lifecycle, trigger_now() submitting one short background job
per cycle, config re-read each cycle -- the simplest matching scheduler
shape in this codebase (no durable cursor / multi-tick pass, unlike the
HNSW orphan sweep). The actual tick logic (delete rows older than the
retention cutoff, respecting the enabled toggle) is tested against a REAL
EmbeddingCallStatsSqliteBackend (Anti-Mock), not a mock.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

_SECONDS_PER_DAY = 86400
_RETENTION_DAYS = 90
_OLD_RECORD_AGE_DAYS = 100  # exceeds retention window
_RECENT_RECORD_AGE_DAYS = 10  # within retention window
_ANOTHER_RECENT_RECORD_AGE_DAYS = 5  # within retention window


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_background_job_manager():
    mgr = MagicMock()
    mgr.submit_job.return_value = "job-001"
    return mgr


@pytest.fixture
def mock_config_service_enabled():
    svc = MagicMock()
    cfg = svc.get_config.return_value.embedding_stats_config
    cfg.enabled = True
    cfg.retention_days = _RETENTION_DAYS
    return svc


@pytest.fixture
def real_backend(tmp_path):
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsSqliteBackend,
    )

    return EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))


def _insert_record(backend, occurred_at: float):
    from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

    backend.insert_batch(
        [
            EmbeddingCallRecord(
                provider="voyageai",
                call_type="embed",
                model="voyage-code-3",
                item_count=1,
                token_count=10,
                batch_size=1,
                purpose="index",
                success=True,
                latency_ms=5,
                occurred_at=occurred_at,
            )
        ]
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestSchedulerLifecycle:
    def test_start_creates_daemon_thread(
        self, real_backend, mock_background_job_manager, mock_config_service_enabled
    ):
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_enabled,
        )
        scheduler.start()

        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
        scheduler.stop()

    def test_stop_exits_within_timeout(
        self, real_backend, mock_background_job_manager, mock_config_service_enabled
    ):
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_enabled,
        )
        scheduler.start()
        scheduler.stop()

        assert scheduler._thread is None or not scheduler._thread.is_alive()

    def test_double_stop_is_safe(
        self, real_backend, mock_background_job_manager, mock_config_service_enabled
    ):
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_enabled,
        )
        scheduler.start()
        scheduler.stop()
        scheduler.stop()  # must not raise


# ---------------------------------------------------------------------------
# trigger_now
# ---------------------------------------------------------------------------


class TestSchedulerTriggerNow:
    def test_trigger_now_submits_job_with_correct_operation_type(
        self, real_backend, mock_background_job_manager, mock_config_service_enabled
    ):
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_enabled,
        )

        scheduler.trigger_now()

        mock_background_job_manager.submit_job.assert_called_once()
        args = mock_background_job_manager.submit_job.call_args
        assert args[0][0] == "embedding_stats_retention_sweep"
        assert args[1]["submitter_username"] == "system"
        assert args[1]["is_admin"] is True

    def test_trigger_now_returns_job_id_without_requiring_start(
        self, real_backend, mock_background_job_manager, mock_config_service_enabled
    ):
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        mock_background_job_manager.submit_job.return_value = "job-trigger-xyz"

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_enabled,
        )

        job_id = scheduler.trigger_now()

        assert job_id == "job-trigger-xyz"


# ---------------------------------------------------------------------------
# Config re-read each cycle
# ---------------------------------------------------------------------------


class TestSchedulerConfigReread:
    def test_run_tick_reads_config_fresh_on_every_invocation(
        self, real_backend, mock_background_job_manager
    ):
        """_run_tick() must call config_service.get_config() on EACH
        invocation (never cache enabled/retention_days from a prior call)
        -- proven deterministically by invoking it twice and observing the
        call count increase, mirroring ActivatedReaperScheduler's AC4
        cadence-reread regression guard without depending on a mocked
        background_job_manager actually executing the submitted job body
        (MagicMock.submit_job is a no-op by default, unlike the real
        BackgroundJobManager)."""
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        config_service = MagicMock()
        cfg = config_service.get_config.return_value.embedding_stats_config
        cfg.enabled = True
        cfg.retention_days = _RETENTION_DAYS

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )

        scheduler._run_tick()
        assert config_service.get_config.call_count == 1

        scheduler._run_tick()
        assert config_service.get_config.call_count == 2


# ---------------------------------------------------------------------------
# Tick logic -- real backend, Anti-Mock
# ---------------------------------------------------------------------------


class TestTickDeletesOldRecords:
    def test_deletes_rows_older_than_retention_cutoff(
        self, real_backend, mock_background_job_manager
    ):
        now = time.time()
        _insert_record(real_backend, now - (_OLD_RECORD_AGE_DAYS * _SECONDS_PER_DAY))
        _insert_record(real_backend, now - (_RECENT_RECORD_AGE_DAYS * _SECONDS_PER_DAY))

        config_service = MagicMock()
        cfg = config_service.get_config.return_value.embedding_stats_config
        cfg.enabled = True
        cfg.retention_days = _RETENTION_DAYS

        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )
        scheduler._run_tick()

        remaining = real_backend.query(limit=10)
        assert len(remaining) == 1
        assert remaining[0].occurred_at == pytest.approx(
            now - (_RECENT_RECORD_AGE_DAYS * _SECONDS_PER_DAY), abs=5
        )

    def test_keeps_all_rows_when_none_exceed_retention_window(
        self, real_backend, mock_background_job_manager
    ):
        now = time.time()
        _insert_record(
            real_backend, now - (_ANOTHER_RECENT_RECORD_AGE_DAYS * _SECONDS_PER_DAY)
        )
        _insert_record(real_backend, now - (_RECENT_RECORD_AGE_DAYS * _SECONDS_PER_DAY))

        config_service = MagicMock()
        cfg = config_service.get_config.return_value.embedding_stats_config
        cfg.enabled = True
        cfg.retention_days = _RETENTION_DAYS

        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )
        scheduler._run_tick()

        assert len(real_backend.query(limit=10)) == 2


class TestTickRespectsEnabledToggle:
    def test_disabled_config_skips_deletion_entirely(
        self, real_backend, mock_background_job_manager
    ):
        now = time.time()
        _insert_record(
            real_backend, now - (_OLD_RECORD_AGE_DAYS * _SECONDS_PER_DAY)
        )  # would be deleted if enabled

        config_service = MagicMock()
        cfg = config_service.get_config.return_value.embedding_stats_config
        cfg.enabled = False
        cfg.retention_days = _RETENTION_DAYS

        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )
        scheduler._run_tick()

        assert len(real_backend.query(limit=10)) == 1


class TestTriggerNowDuplicateJobError:
    def test_duplicate_job_error_returns_none_without_raising(
        self, real_backend, mock_config_service_enabled
    ):
        """Another worker already claimed this tick -- benign, expected in
        multi-worker deployments (mirrors ActivatedReaperScheduler)."""
        from code_indexer.server.repositories.background_jobs import (
            DuplicateJobError,
        )
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        job_manager = MagicMock()
        job_manager.submit_job.side_effect = DuplicateJobError(
            operation_type="embedding_stats_retention_sweep",
            repo_alias="server",
            existing_job_id="job-already-running",
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=job_manager,
            config_service=mock_config_service_enabled,
        )

        result = scheduler.trigger_now()

        assert result is None


class TestRunTickFailsOpenOnConfigReadFailure:
    def test_config_read_exception_skips_cycle_without_raising(
        self, real_backend, mock_background_job_manager
    ):
        """A raising config_service.get_config() must not crash _run_tick()
        or delete any rows -- fail-open, skip this cycle."""
        now = time.time()
        _insert_record(real_backend, now - (_OLD_RECORD_AGE_DAYS * _SECONDS_PER_DAY))

        config_service = MagicMock()
        config_service.get_config.side_effect = RuntimeError("config unavailable")

        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )

        scheduler._run_tick()  # must not raise

        assert len(real_backend.query(limit=10)) == 1  # nothing deleted


class TestLoopSurvivesTriggerNowFailure:
    def test_loop_thread_stays_alive_after_trigger_now_raises(
        self, real_backend, mock_config_service_enabled
    ):
        """_loop()'s try/except around trigger_now() must catch an
        unexpected exception (not DuplicateJobError) and keep the daemon
        thread running, mirroring ActivatedReaperScheduler's fail-soft
        loop."""
        from code_indexer.server.services.embedding_stats_retention_scheduler import (
            EmbeddingStatsRetentionScheduler,
        )

        job_manager = MagicMock()
        job_manager.submit_job.side_effect = RuntimeError("boom")

        scheduler = EmbeddingStatsRetentionScheduler(
            backend=real_backend,
            background_job_manager=job_manager,
            config_service=mock_config_service_enabled,
        )
        scheduler.start()
        try:
            assert scheduler._thread is not None
            assert scheduler._thread.is_alive()
        finally:
            scheduler.stop()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
