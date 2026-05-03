"""
Integration test for Story #967: ActivatedReaperService + BackgroundJobManager round-trip.

Tests the full submit_job → execute → persist → get_job_status flow using a real
BackgroundJobManager and real ActivatedReaperService.  No mocking of the persistence
layer.

Key invariants verified:
  - ReapCycleResult is JSON-serializable (fix for json.dumps failure on dataclass)
  - job.result is a plain dict with 'scanned', 'reaped', 'skipped', 'errors' keys
  - Job status transitions: PENDING → RUNNING → COMPLETED
  - get_job_status returns the result without raising
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.services.activated_reaper_service import ActivatedReaperService
from code_indexer.server.services.activated_reaper_scheduler import (
    ActivatedReaperScheduler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_repo(
    username: str, user_alias: str, last_accessed: Optional[str]
) -> Dict[str, Any]:
    return {
        "username": username,
        "user_alias": user_alias,
        "last_accessed": last_accessed,
        "golden_repo_alias": "golden-repo",
    }


def _wait_for_job(
    manager: BackgroundJobManager,
    job_id: str,
    timeout_seconds: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """Poll get_job_status until job is no longer PENDING or RUNNING."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status: Optional[Dict[str, Any]] = manager.get_job_status(
            job_id, username="system"
        )
        if status is None:
            time.sleep(0.05)
            continue
        job_status = status.get("status")
        if job_status not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
            return status
        time.sleep(0.05)
    final: Optional[Dict[str, Any]] = manager.get_job_status(job_id, username="system")
    return final


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_job_manager():
    """Real BackgroundJobManager with no SQLite backend (in-memory only)."""
    return BackgroundJobManager()


@pytest.fixture
def mock_config_service_30d():
    """ConfigService stub with ttl_days=30."""
    svc = MagicMock()
    svc.get_config.return_value.activated_reaper_config.ttl_days = 30
    svc.get_config.return_value.activated_reaper_config.cadence_hours = 9999
    return svc


# ---------------------------------------------------------------------------
# Integration: no repos → COMPLETED with zero counts
# ---------------------------------------------------------------------------


class TestReaperJobRoundTripEmpty:
    """Full round-trip with no repos: job completes, result is a serializable dict."""

    def test_empty_repo_list_job_completes(
        self, real_job_manager, mock_config_service_30d
    ):
        """submit_job → execute → COMPLETED with zero-count dict result."""
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_all_activated_repositories.return_value = []

        service = ActivatedReaperService(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )
        scheduler = ActivatedReaperScheduler(
            service=service,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )

        job_id = scheduler.trigger_now()

        assert job_id is not None
        status = _wait_for_job(real_job_manager, job_id)

        assert status is not None
        assert status["status"] == JobStatus.COMPLETED.value

    def test_result_is_plain_dict_not_dataclass(
        self, real_job_manager, mock_config_service_30d
    ):
        """job.result must be a plain dict (JSON-serializable), not a ReapCycleResult."""
        import json

        activated_repo_manager = MagicMock()
        activated_repo_manager.list_all_activated_repositories.return_value = []

        service = ActivatedReaperService(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )
        scheduler = ActivatedReaperScheduler(
            service=service,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )

        job_id = scheduler.trigger_now()
        status = _wait_for_job(real_job_manager, job_id)

        assert status is not None
        result = status.get("result")
        assert result is not None
        # Must be a dict, not a dataclass
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        # Must be JSON-serializable (the core invariant this test enforces)
        serialized = json.dumps(result)
        assert serialized is not None

    def test_result_has_required_keys(self, real_job_manager, mock_config_service_30d):
        """result dict must contain scanned, reaped, skipped, errors keys."""
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_all_activated_repositories.return_value = []

        service = ActivatedReaperService(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )
        scheduler = ActivatedReaperScheduler(
            service=service,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )

        job_id = scheduler.trigger_now()
        status = _wait_for_job(real_job_manager, job_id)

        assert status is not None
        result = status["result"]
        assert "scanned" in result
        assert "reaped" in result
        assert "skipped" in result
        assert "errors" in result
        assert result["scanned"] == 0
        assert result["reaped"] == []
        assert result["skipped"] == []
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# Integration: expired repos → reaped entries in result
# ---------------------------------------------------------------------------


class TestReaperJobRoundTripWithRepos:
    """Full round-trip with repos present."""

    def test_expired_repo_appears_in_reaped(
        self, real_job_manager, mock_config_service_30d
    ):
        """Expired repo ends up in result['reaped'] after job completes."""
        old_ts = _iso(_utcnow() - timedelta(days=40))
        repos = [_make_repo("alice", "old-repo", old_ts)]

        activated_repo_manager = MagicMock()
        activated_repo_manager.list_all_activated_repositories.return_value = repos

        # Use a separate mock for deactivation submissions (inner job manager)
        inner_job_manager = MagicMock()
        inner_job_manager.submit_job.return_value = "inner-job-001"

        service = ActivatedReaperService(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=inner_job_manager,
            config_service=mock_config_service_30d,
        )
        # outer real manager submits/executes the reap cycle job
        scheduler = ActivatedReaperScheduler(
            service=service,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )

        job_id = scheduler.trigger_now()
        status = _wait_for_job(real_job_manager, job_id)

        assert status is not None
        assert status["status"] == JobStatus.COMPLETED.value
        result = status["result"]
        assert result["scanned"] == 1
        assert len(result["reaped"]) == 1
        assert result["reaped"][0]["username"] == "alice"
        assert result["reaped"][0]["user_alias"] == "old-repo"

    def test_recent_repo_appears_in_skipped(
        self, real_job_manager, mock_config_service_30d
    ):
        """Recent repo ends up in result['skipped'] after job completes."""
        recent_ts = _iso(_utcnow() - timedelta(days=5))
        repos = [_make_repo("bob", "fresh-repo", recent_ts)]

        activated_repo_manager = MagicMock()
        activated_repo_manager.list_all_activated_repositories.return_value = repos

        service = ActivatedReaperService(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=MagicMock(),
            config_service=mock_config_service_30d,
        )
        scheduler = ActivatedReaperScheduler(
            service=service,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )

        job_id = scheduler.trigger_now()
        status = _wait_for_job(real_job_manager, job_id)

        assert status is not None
        result = status["result"]
        assert result["scanned"] == 1
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["username"] == "bob"


# ---------------------------------------------------------------------------
# Integration: get_job_status called after completion
# ---------------------------------------------------------------------------


class TestReaperJobStatusLookup:
    """get_job_status returns the completed job with result."""

    def test_get_job_status_returns_completed_job(
        self, real_job_manager, mock_config_service_30d
    ):
        """get_job_status(job_id, 'system') returns COMPLETED status with result dict."""
        activated_repo_manager = MagicMock()
        activated_repo_manager.list_all_activated_repositories.return_value = []

        service = ActivatedReaperService(
            activated_repo_manager=activated_repo_manager,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )
        scheduler = ActivatedReaperScheduler(
            service=service,
            background_job_manager=real_job_manager,
            config_service=mock_config_service_30d,
        )

        job_id = scheduler.trigger_now()
        _wait_for_job(real_job_manager, job_id)

        final_status = real_job_manager.get_job_status(job_id, username="system")

        assert final_status is not None
        assert final_status["job_id"] == job_id
        assert final_status["status"] == JobStatus.COMPLETED.value
        assert final_status["operation_type"] == "reap_activated_repos"
        assert isinstance(final_status["result"], dict)
