"""
Unit tests for DependencyMapDashboardCacheBackend (Story #684).

Uses real SQLite with a temporary file-backed database (anti-mock methodology).
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

import pytest

from code_indexer.server.storage.sqlite_backends import (
    DependencyMapDashboardCacheBackend,
)
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TTL_SECONDS = 600
STALE_SECONDS = TTL_SECONDS + 100
SAMPLE_RESULT = {"health": "Healthy", "color": "GREEN"}
SAMPLE_RESULT_JSON = json.dumps(SAMPLE_RESULT)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """Real SQLite DB path in a temp directory."""
    return str(tmp_path / "test_cache.db")


@pytest.fixture
def backend(db_path):
    """DependencyMapDashboardCacheBackend backed by a real SQLite file."""
    return DependencyMapDashboardCacheBackend(db_path)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def assert_recent_iso(value: str, before: datetime, after: datetime) -> None:
    """Assert an ISO timestamp string falls within [before, after]."""
    assert value is not None
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    assert before <= ts <= after


def seed_cached(backend: DependencyMapDashboardCacheBackend) -> None:
    """Seed backend with a valid cached result using SAMPLE_RESULT_JSON."""
    backend.set_cached(SAMPLE_RESULT_JSON, "job-seed")


def seed_claimed(
    backend: DependencyMapDashboardCacheBackend, job_id: str = "job-1"
) -> None:
    """Seed backend with a claimed (in-flight) job slot."""
    backend.claim_job_slot(job_id)


def assert_failure_fields_cleared(backend: DependencyMapDashboardCacheBackend) -> None:
    """Assert last_failure_message and last_failure_at are both None."""
    cached = backend.get_cached()
    assert cached is not None
    assert cached["last_failure_message"] is None
    assert cached["last_failure_at"] is None


def assert_job_id_cleared(backend: DependencyMapDashboardCacheBackend) -> None:
    """Assert job_id is None in the current cached row."""
    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None


def make_stale(backend: DependencyMapDashboardCacheBackend) -> None:
    """Force computed_at to be STALE_SECONDS ago in the database."""
    conn_mgr = DatabaseConnectionManager.get_instance(backend._db_path)
    old_time = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)
    ).isoformat()

    def _update(conn):
        conn.execute(
            "UPDATE dependency_map_dashboard_cache SET computed_at = ? WHERE cache_key = 'default'",
            (old_time,),
        )

    conn_mgr.execute_atomic(_update)


def make_tracker(status: str) -> Mock:
    """Return a mock JobTracker whose get_job returns a job with the given status."""
    tracker = Mock()
    tracker.get_job.return_value = Mock(status=status)
    return tracker


# ─────────────────────────────────────────────────────────────────────────────
# TestInitialState
# ─────────────────────────────────────────────────────────────────────────────


class TestInitialState:
    def test_get_cached_returns_none(self, backend):
        assert backend.get_cached() is None

    def test_is_fresh_returns_false(self, backend):
        assert backend.is_fresh(ttl_seconds=TTL_SECONDS) is False

    def test_get_running_job_id_returns_none(self, backend):
        assert backend.get_running_job_id() is None


# ─────────────────────────────────────────────────────────────────────────────
# TestSetAndGetCached
# ─────────────────────────────────────────────────────────────────────────────


class TestSetAndGetCached:
    def test_stores_result_json(self, backend):
        backend.set_cached(SAMPLE_RESULT_JSON, "job-1")
        assert json.loads(backend.get_cached()["result_json"]) == SAMPLE_RESULT

    @pytest.mark.parametrize(
        "field", ["job_id", "last_failure_message", "last_failure_at"]
    )
    def test_clears_transient_fields_parametrized(self, backend, field):
        backend.mark_job_failed("prior error")
        backend.set_cached(SAMPLE_RESULT_JSON, "job-2")
        assert backend.get_cached()[field] is None

    def test_sets_computed_at_recently(self, backend):
        before = datetime.now(timezone.utc)
        backend.set_cached(SAMPLE_RESULT_JSON, "job-1")
        after = datetime.now(timezone.utc)
        assert_recent_iso(backend.get_cached()["computed_at"], before, after)


# ─────────────────────────────────────────────────────────────────────────────
# TestIsFresh
# ─────────────────────────────────────────────────────────────────────────────


class TestIsFresh:
    def test_true_when_recently_computed(self, backend):
        seed_cached(backend)
        assert backend.is_fresh(ttl_seconds=TTL_SECONDS) is True

    def test_false_when_no_computed_at(self, backend):
        seed_claimed(backend)
        assert backend.is_fresh(ttl_seconds=TTL_SECONDS) is False

    def test_false_when_stale(self, backend):
        seed_cached(backend)
        make_stale(backend)
        assert backend.is_fresh(ttl_seconds=TTL_SECONDS) is False


# ─────────────────────────────────────────────────────────────────────────────
# TestClaimJobSlot
# ─────────────────────────────────────────────────────────────────────────────


class TestClaimJobSlot:
    def test_first_claim_succeeds(self, backend):
        result = backend.claim_job_slot("job-1")
        assert result is None
        assert backend.get_cached()["job_id"] == "job-1"

    def test_second_claim_returns_winner(self, backend):
        seed_claimed(backend, "job-1")
        result = backend.claim_job_slot("job-2")
        assert result == "job-1"
        assert backend.get_cached()["job_id"] == "job-1"

    @pytest.mark.parametrize("release", ["clear", "set_cached"])
    def test_claim_after_release_succeeds(self, backend, release):
        seed_claimed(backend, "job-1")
        if release == "clear":
            backend.clear_job_slot()
        else:
            backend.set_cached(SAMPLE_RESULT_JSON, "job-1")
        result = backend.claim_job_slot("job-2")
        assert result is None
        assert backend.get_cached()["job_id"] == "job-2"


# ─────────────────────────────────────────────────────────────────────────────
# TestMarkJobFailed
# ─────────────────────────────────────────────────────────────────────────────


class TestMarkJobFailed:
    def test_sets_failure_fields(self, backend):
        before = datetime.now(timezone.utc)
        backend.mark_job_failed("timeout")
        after = datetime.now(timezone.utc)
        cached = backend.get_cached()
        assert cached["last_failure_message"] == "timeout"
        assert_recent_iso(cached["last_failure_at"], before, after)

    def test_clears_job_id_and_preserves_result(self, backend):
        seed_cached(backend)
        seed_claimed(backend, "job-1")
        backend.mark_job_failed("error")
        cached = backend.get_cached()
        assert cached["job_id"] is None
        assert json.loads(cached["result_json"]) == SAMPLE_RESULT

    def test_creates_row_if_missing(self, backend):
        backend.mark_job_failed("cold start error")
        assert backend.get_cached()["last_failure_message"] == "cold start error"


# ─────────────────────────────────────────────────────────────────────────────
# TestClearJobSlotForRetry
# ─────────────────────────────────────────────────────────────────────────────


class TestClearJobSlotForRetry:
    def test_clears_job_id(self, backend):
        seed_claimed(backend, "job-1")
        backend.clear_job_slot_for_retry()
        assert_job_id_cleared(backend)

    @pytest.mark.parametrize("field", ["last_failure_message", "last_failure_at"])
    def test_clears_failure_fields_parametrized(self, backend, field):
        backend.mark_job_failed("prior error")
        backend.clear_job_slot_for_retry()
        assert backend.get_cached()[field] is None

    def test_preserves_cached_result(self, backend):
        seed_cached(backend)
        backend.mark_job_failed("error")
        backend.clear_job_slot_for_retry()
        assert json.loads(backend.get_cached()["result_json"]) == SAMPLE_RESULT


# ─────────────────────────────────────────────────────────────────────────────
# TestGetRunningJobId
# ─────────────────────────────────────────────────────────────────────────────


class TestGetRunningJobId:
    def test_returns_job_id_for_live_job(self, backend):
        seed_claimed(backend, "job-alive")
        result = backend.get_running_job_id(job_tracker=make_tracker("running"))
        assert result == "job-alive"

    @pytest.mark.parametrize("zombie_status", ["completed", "failed", None])
    def test_clears_slot_and_returns_none_for_zombie(self, backend, zombie_status):
        seed_claimed(backend, "job-zombie")
        tracker = Mock()
        tracker.get_job.return_value = (
            Mock(status=zombie_status) if zombie_status is not None else None
        )
        result = backend.get_running_job_id(job_tracker=tracker)
        assert result is None
        assert_job_id_cleared(backend)

    @pytest.mark.parametrize("scenario", ["no_tracker", "tracker_raises"])
    def test_safe_fallback_without_tracker(self, backend, scenario):
        seed_claimed(backend, "job-1")
        if scenario == "no_tracker":
            result = backend.get_running_job_id(job_tracker=None)
        else:
            tracker = Mock()
            tracker.get_job.side_effect = Exception("unavailable")
            result = backend.get_running_job_id(job_tracker=tracker)
        assert result == "job-1"
