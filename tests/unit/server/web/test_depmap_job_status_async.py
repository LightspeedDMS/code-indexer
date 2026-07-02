"""
Unit tests for Story #684: async state-machine in depmap_job_status_partial.

Four states tested (in endpoint evaluation order):
  STATE 1: fresh cache -> complete template, no job submitted
  STATE 2: job_id param polling -> route by job.status (checked before in-flight)
  STATE 3: in-flight job running -> computing template, no new job submitted
  STATE 4: no cache, no job -> submit new job via background job manager

Also covers the retry endpoint (POST).

Patching seams (module-level accessors only):
  _get_dashboard_cache_backend  - controls cache state
  _get_job_tracker              - controls tracked job state
  _get_dashboard_service        - controls the dashboard computation service
  _get_background_job_manager   - controls background job submission

Internal helpers are NOT patched; those code paths run normally.

Auth regression: test_dependency_map_routes.py
Content-health regression: test_depmap_running_state_bugs.py
"""

import json
import re
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ─────────────────────────────────────────────────────────────────────────────
# Module constants
# ─────────────────────────────────────────────────────────────────────────────

# Endpoints
_ENDPOINT = "/admin/partials/depmap-job-status"
_RETRY_ENDPOINT = "/admin/partials/depmap-job-status/retry"

# Patch-target prefix
_ROUTES = "code_indexer.server.web.dependency_map_routes"

# Auth — admin/admin is the documented test-only server default (CLAUDE.md)
_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "admin"

# HTTP status codes used in assertions
_HTTP_OK = 200
_HTTP_REDIRECT = 303

# Job IDs
_IN_FLIGHT_JOB_ID = "in-flight-job-001"
_POLL_JOB_ID = "poll-job-002"
_NEW_JOB_ID = "new-bg-job-003"
_RETRY_JOB_ID = _NEW_JOB_ID  # retry path produces a new job via the same fake manager

# Job status strings
_STATUS_PENDING = "pending"
_STATUS_RUNNING = "running"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"

# Progress values
_RUNNING_PROGRESS = 42
_RUNNING_PROGRESS_INFO = "21/50"
_POLL_RUNNING_PROGRESS = 55
_POLL_RUNNING_PROGRESS_INFO = "11/20"
_COMPLETE_PROGRESS = 100

# Progress callback arguments used in FakeDashboardService
_CALLBACK_DONE = 1
_CALLBACK_TOTAL = 1

# Timestamp used in cached-row fixtures
_COMPUTED_AT = "2026-01-01T00:00:00+00:00"

# Sample cached result (mirrors what DependencyMapDashboardService returns)
_SAMPLE_RESULT_DICT: Dict[str, Any] = {
    "health": "Healthy",
    "color": "GREEN",
    "status": "idle",
    "last_run": None,
    "next_run": None,
    "error_message": None,
    "run_history": [],
}

_FRESH_CACHED_ROW: Dict[str, Any] = {
    "result_json": json.dumps(_SAMPLE_RESULT_DICT),
    "computed_at": _COMPUTED_AT,
    "job_id": None,
    "last_failure_message": None,
    "last_failure_at": None,
}


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles (no Mock, no threads)
# ─────────────────────────────────────────────────────────────────────────────


class FakeCacheBackend:
    """Stand-in for DependencyMapDashboardCacheBackend (no SQLite, no threads)."""

    def __init__(
        self,
        fresh: bool = False,
        cached_row: Optional[Dict[str, Any]] = None,
        running_job_id: Optional[str] = None,
    ):
        self._fresh = fresh
        self._cached_row = cached_row
        self._running_job_id = running_job_id
        self.claimed_job_ids: list = []
        self.retry_cleared: bool = False
        # None == claim succeeds; a string == slot already taken
        self._claim_returns: Optional[str] = None

    def is_fresh(self, ttl_seconds: int) -> bool:
        return self._fresh

    def get_cached(self) -> Optional[Dict[str, Any]]:
        return self._cached_row

    def get_running_job_id(self, job_tracker=None) -> Optional[str]:
        return self._running_job_id

    def claim_job_slot(self, new_job_id: str) -> Optional[str]:
        self.claimed_job_ids.append(new_job_id)
        return self._claim_returns

    def clear_job_slot_for_retry(self) -> None:
        self.retry_cleared = True

    def clear_job_slot(self) -> None:
        pass


class FakeTrackedJob:
    """Minimal TrackedJob stand-in (no thread interaction)."""

    def __init__(
        self,
        status: str,
        progress: int = 0,
        progress_info: str = "",
        error: Optional[str] = None,
    ):
        self.status = status
        self.progress = progress
        self.progress_info = progress_info
        self.error = error


class FakeJobTracker:
    """Stand-in for JobTracker (single-threaded, no locks needed)."""

    def __init__(self, jobs: Optional[Dict[str, FakeTrackedJob]] = None):
        self._jobs: Dict[str, FakeTrackedJob] = jobs or {}

    def get_job(self, job_id: str) -> Optional[FakeTrackedJob]:
        return self._jobs.get(job_id)

    def register_job(
        self, job_id: str, operation_type: str, username: str, **kwargs
    ) -> None:
        if job_id not in self._jobs:
            self._jobs[job_id] = FakeTrackedJob(status=_STATUS_PENDING)

    def update_status(self, job_id: str, **kwargs) -> None:
        if job_id in self._jobs and "status" in kwargs:
            self._jobs[job_id].status = kwargs["status"]


class FakeBgJobManager:
    """
    Stand-in for BackgroundJobManager.

    submit_job records the call and returns the configured job_id without
    spawning any threads, eliminating shared-mutable-state concurrency risks.
    """

    def __init__(self, job_id: str = _NEW_JOB_ID):
        self._job_id = job_id
        self.submitted: list = []

    def submit_job(
        self,
        operation_type: str,
        func,
        *args,
        submitter_username: str = _ADMIN_USERNAME,
        is_admin: bool = False,
        repo_alias: Optional[str] = None,
        **kwargs,
    ) -> str:
        self.submitted.append(
            {"operation_type": operation_type, "repo_alias": repo_alias}
        )
        return self._job_id


class FakeDashboardService:
    """Minimal stand-in for DependencyMapDashboardService."""

    def get_job_status(self, progress_callback=None) -> Dict[str, Any]:
        if progress_callback is not None:
            progress_callback(_CALLBACK_DONE, _CALLBACK_TOTAL)
        return dict(_SAMPLE_RESULT_DICT)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — module-scoped; admin session cookie set on the client instance
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app():
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture(scope="module")
def client(app):
    """
    Yield a TestClient with the admin session cookie pre-set on the instance.

    Using TestClient as a context manager guarantees app-lifespan cleanup.
    Cookies are set directly on the client (not per-request) per starlette
    guidance; admin/admin is the documented test-only default (CLAUDE.md).
    """
    with TestClient(app) as tc:
        login_page = tc.get("/login")
        assert login_page.status_code == _HTTP_OK
        match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        assert match, "Could not extract CSRF token"
        csrf_token = match.group(1)
        resp = tc.post(
            "/login",
            data={
                "username": _ADMIN_USERNAME,
                "password": _ADMIN_PASSWORD,
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == _HTTP_REDIRECT
        assert "session" in resp.cookies
        tc.cookies.set("session", resp.cookies["session"])
        yield tc


# ─────────────────────────────────────────────────────────────────────────────
# Request helpers — no per-request cookie injection (client.cookies used)
# ─────────────────────────────────────────────────────────────────────────────


def _get_status(
    client: TestClient,
    cache_backend: FakeCacheBackend,
    tracker: FakeJobTracker,
    bg_manager: FakeBgJobManager,
    dashboard_service: FakeDashboardService,
    job_id: Optional[str] = None,
):
    """
    GET /admin/partials/depmap-job-status with all four external seams replaced.
    Session auth is taken from client.cookies (set at fixture creation time).
    Returns the Response object.
    """
    url = _ENDPOINT if job_id is None else f"{_ENDPOINT}?job_id={job_id}"
    with (
        patch(f"{_ROUTES}._get_dashboard_cache_backend", return_value=cache_backend),
        patch(f"{_ROUTES}._get_job_tracker", return_value=tracker),
        patch(f"{_ROUTES}._get_background_job_manager", return_value=bg_manager),
        patch(f"{_ROUTES}._get_dashboard_service", return_value=dashboard_service),
        # Bypass Story #342 live content-health merge so fixture values are preserved.
        # That code path is tested separately in test_depmap_running_state_bugs.py.
        patch(f"{_ROUTES}._get_dep_map_output_dir", return_value=None),
    ):
        return client.get(url)


def _post_retry(
    client: TestClient,
    cache_backend: FakeCacheBackend,
    tracker: FakeJobTracker,
    bg_manager: FakeBgJobManager,
    dashboard_service: FakeDashboardService,
):
    """POST /admin/partials/depmap-job-status/retry with all four seams replaced."""
    with (
        patch(f"{_ROUTES}._get_dashboard_cache_backend", return_value=cache_backend),
        patch(f"{_ROUTES}._get_job_tracker", return_value=tracker),
        patch(f"{_ROUTES}._get_background_job_manager", return_value=bg_manager),
        patch(f"{_ROUTES}._get_dashboard_service", return_value=dashboard_service),
        # Bypass Story #342 live content-health merge so fixture values are preserved.
        # That code path is tested separately in test_depmap_running_state_bugs.py.
        patch(f"{_ROUTES}._get_dep_map_output_dir", return_value=None),
    ):
        return client.post(_RETRY_ENDPOINT)


# ─────────────────────────────────────────────────────────────────────────────
# STATE 1: fresh cache -> complete template
# ─────────────────────────────────────────────────────────────────────────────


class TestState1FreshCache:
    """Fresh cache -> complete template rendered, no job submitted."""

    def _fresh_backend(self) -> FakeCacheBackend:
        return FakeCacheBackend(fresh=True, cached_row=_FRESH_CACHED_ROW)

    def test_returns_200_with_job_status_div(self, client):
        resp = _get_status(
            client,
            self._fresh_backend(),
            FakeJobTracker(),
            FakeBgJobManager(),
            FakeDashboardService(),
        )
        assert resp.status_code == _HTTP_OK
        assert "depmap-job-status" in resp.text

    def test_no_job_submitted_for_fresh_cache(self, client):
        bg = FakeBgJobManager()
        _get_status(
            client,
            self._fresh_backend(),
            FakeJobTracker(),
            bg,
            FakeDashboardService(),
        )
        assert bg.submitted == [], "No job must be submitted for a fresh cache"

    def test_cached_health_value_appears_in_response(self, client):
        resp = _get_status(
            client,
            self._fresh_backend(),
            FakeJobTracker(),
            FakeBgJobManager(),
            FakeDashboardService(),
        )
        assert "Healthy" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# STATE 2: job_id param polling -> route by job status
# ─────────────────────────────────────────────────────────────────────────────


class TestState2JobIdPolling:
    """job_id query param present -> routing by job status (before in-flight check)."""

    def test_completed_job_returns_complete_view(self, client):
        completed_job = FakeTrackedJob(
            status=_STATUS_COMPLETED, progress=_COMPLETE_PROGRESS
        )
        tracker = FakeJobTracker(jobs={_POLL_JOB_ID: completed_job})
        cached_row = {**_FRESH_CACHED_ROW, "job_id": None}
        backend = FakeCacheBackend(
            fresh=False, cached_row=cached_row, running_job_id=None
        )

        resp = _get_status(
            client,
            backend,
            tracker,
            FakeBgJobManager(),
            FakeDashboardService(),
            job_id=_POLL_JOB_ID,
        )
        assert resp.status_code == _HTTP_OK
        assert "depmap-job-status" in resp.text
        assert "Processing" not in resp.text

    def test_failed_job_returns_error_partial(self, client):
        failed_job = FakeTrackedJob(status=_STATUS_FAILED, error="Analysis exploded")
        tracker = FakeJobTracker(jobs={_POLL_JOB_ID: failed_job})
        backend = FakeCacheBackend(fresh=False, cached_row=None, running_job_id=None)

        resp = _get_status(
            client,
            backend,
            tracker,
            FakeBgJobManager(),
            FakeDashboardService(),
            job_id=_POLL_JOB_ID,
        )
        assert resp.status_code == _HTTP_OK
        content = resp.text
        assert "Analysis exploded" in content or _STATUS_FAILED in content.lower()
        assert "Retry" in content or "retry" in content.lower()

    def test_running_job_returns_computing_partial(self, client):
        running_job = FakeTrackedJob(
            status=_STATUS_RUNNING,
            progress=_POLL_RUNNING_PROGRESS,
            progress_info=_POLL_RUNNING_PROGRESS_INFO,
        )
        tracker = FakeJobTracker(jobs={_POLL_JOB_ID: running_job})
        backend = FakeCacheBackend(
            fresh=False, cached_row=None, running_job_id=_POLL_JOB_ID
        )

        resp = _get_status(
            client,
            backend,
            tracker,
            FakeBgJobManager(),
            FakeDashboardService(),
            job_id=_POLL_JOB_ID,
        )
        assert resp.status_code == _HTTP_OK
        assert "depmap-job-status" in resp.text
        assert _POLL_JOB_ID in resp.text or "Processing" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# STATE 3: in-flight job -> computing template
# ─────────────────────────────────────────────────────────────────────────────


class TestState3InFlightJob:
    """Stale cache but a job is running -> computing template, no new submission."""

    def _setup(self):
        job = FakeTrackedJob(
            status=_STATUS_RUNNING,
            progress=_RUNNING_PROGRESS,
            progress_info=_RUNNING_PROGRESS_INFO,
        )
        backend = FakeCacheBackend(
            fresh=False, cached_row=None, running_job_id=_IN_FLIGHT_JOB_ID
        )
        tracker = FakeJobTracker(jobs={_IN_FLIGHT_JOB_ID: job})
        return backend, tracker

    def test_returns_200_with_computing_html(self, client):
        backend, tracker = self._setup()
        resp = _get_status(
            client,
            backend,
            tracker,
            FakeBgJobManager(),
            FakeDashboardService(),
        )
        assert resp.status_code == _HTTP_OK
        assert "depmap-job-status" in resp.text

    def test_no_new_job_submitted_when_in_flight(self, client):
        backend, tracker = self._setup()
        bg = FakeBgJobManager()
        _get_status(client, backend, tracker, bg, FakeDashboardService())
        assert bg.submitted == [], "Must not submit new job when one is already running"

    def test_progress_value_present(self, client):
        backend, tracker = self._setup()
        resp = _get_status(
            client,
            backend,
            tracker,
            FakeBgJobManager(),
            FakeDashboardService(),
        )
        assert resp.status_code == _HTTP_OK
        assert str(_RUNNING_PROGRESS) in resp.text or "Processing" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# STATE 4: no cache, no job -> submit new background job
# ─────────────────────────────────────────────────────────────────────────────


class TestState4SubmitNewJob:
    """No cache, no in-flight job -> submit background job, return computing partial."""

    def _empty_backend(self) -> FakeCacheBackend:
        return FakeCacheBackend(fresh=False, cached_row=None, running_job_id=None)

    def test_submits_new_job(self, client):
        bg = FakeBgJobManager(job_id=_NEW_JOB_ID)
        _get_status(
            client,
            self._empty_backend(),
            FakeJobTracker(),
            bg,
            FakeDashboardService(),
        )
        assert bg.submitted, "Background job must be submitted"

    def test_returns_computing_partial_with_new_job_id(self, client):
        bg = FakeBgJobManager(job_id=_NEW_JOB_ID)
        resp = _get_status(
            client,
            self._empty_backend(),
            FakeJobTracker(),
            bg,
            FakeDashboardService(),
        )
        assert resp.status_code == _HTTP_OK
        assert "depmap-job-status" in resp.text
        assert _NEW_JOB_ID in resp.text

    def test_computing_partial_includes_polling_trigger(self, client):
        bg = FakeBgJobManager(job_id=_NEW_JOB_ID)
        resp = _get_status(
            client,
            self._empty_backend(),
            FakeJobTracker(),
            bg,
            FakeDashboardService(),
        )
        assert resp.status_code == _HTTP_OK
        assert "hx-trigger" in resp.text or "every" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# Retry endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestRetryEndpoint:
    """POST retry -> clear_job_slot_for_retry, then re-enter state machine."""

    def _empty_backend(self) -> FakeCacheBackend:
        return FakeCacheBackend(fresh=False, cached_row=None)

    def test_retry_clears_job_slot(self, client):
        backend = self._empty_backend()
        _post_retry(
            client,
            backend,
            FakeJobTracker(),
            FakeBgJobManager(_RETRY_JOB_ID),
            FakeDashboardService(),
        )
        assert backend.retry_cleared, "clear_job_slot_for_retry must be called"

    def test_retry_submits_new_job(self, client):
        bg = FakeBgJobManager(job_id=_RETRY_JOB_ID)
        _post_retry(
            client,
            self._empty_backend(),
            FakeJobTracker(),
            bg,
            FakeDashboardService(),
        )
        assert bg.submitted, "New job must be submitted on retry"

    def test_retry_returns_computing_partial_with_job_id(self, client):
        bg = FakeBgJobManager(job_id=_RETRY_JOB_ID)
        resp = _post_retry(
            client,
            self._empty_backend(),
            FakeJobTracker(),
            bg,
            FakeDashboardService(),
        )
        assert resp.status_code == _HTTP_OK
        assert "depmap-job-status" in resp.text
        assert _RETRY_JOB_ID in resp.text
