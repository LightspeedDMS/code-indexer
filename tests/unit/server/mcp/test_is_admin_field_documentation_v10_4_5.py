"""v10.4.5 tests for Defect 4: is_admin field documentation and behaviour.

The BackgroundJob.is_admin field is a job-priority opt-in flag, NOT a reflection
of the submitter's user role. xray_search and xray_explore handlers never pass
is_admin=True to submit_job, so the field is always False in their job results
regardless of whether the submitting user is an admin.

This test documents that intentional behaviour so future readers understand
the field's meaning and know that is_admin=False in an xray job result does
NOT mean the submitting user is not an admin.

Shared helpers declared:
- _make_user(username, role) — user factory
- _make_bjm() — BackgroundJobManager with in-memory backend
- _noop_job(progress_callback) — job function that completes immediately
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)

CREATED_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)
COMPLETION_TIMEOUT_SECONDS = 3.0
POLL_INTERVAL_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(
    username: str = "admin@example.com", role: UserRole = UserRole.ADMIN
) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=CREATED_AT,
    )


def _make_bjm() -> BackgroundJobManager:
    """In-memory BackgroundJobManager — no SQLite, no disk I/O."""
    from code_indexer.server.utils.config_manager import ServerResourceConfig

    return BackgroundJobManager(resource_config=ServerResourceConfig())


def _noop_job(progress_callback: Any) -> Dict[str, Any]:
    """Job function that completes immediately — no side effects."""
    return {"status": "done"}


# ---------------------------------------------------------------------------
# AC: is_admin=False for admin user submitting xray_search job
# ---------------------------------------------------------------------------


def test_xray_search_job_result_is_admin_field_is_false_for_admin_user():
    """xray_search job submitted by an ADMIN user has is_admin=False.

    is_admin is a job-priority opt-in flag — it is NOT the submitter's role.
    xray_search and xray_explore never pass is_admin=True to submit_job,
    so the field is always False in their job results. This test pins that
    behaviour so that is_admin=False cannot be misread as "user is not admin".
    """
    admin_user = _make_user("admin@example.com", UserRole.ADMIN)
    bjm = _make_bjm()

    # Submit as if coming from an xray_search handler (is_admin not passed -> defaults False)
    job_id = bjm.submit_job(
        operation_type="xray_search",
        func=_noop_job,
        submitter_username=admin_user.username,
        repo_alias="test-repo-global",
        # is_admin intentionally omitted — xray handlers never set it
    )

    # Poll until completed; fail explicitly if deadline expires
    final_status: Any = None
    deadline = time.monotonic() + COMPLETION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        final_status = bjm.get_job_status(job_id, admin_user.username)
        if final_status and final_status.get("status") == "completed":
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    # Assertion 1: polling confirmed completion (fails fast on timeout)
    assert final_status is not None and final_status.get("status") == "completed", (
        f"Job did not reach 'completed' within {COMPLETION_TIMEOUT_SECONDS}s. "
        f"Last status: {final_status}"
    )

    job = bjm.jobs.get(job_id)

    # Assertion 2: job exists in manager's job dict
    assert job is not None

    # Assertion 3: BackgroundJob object itself reflects completion
    assert job.status == JobStatus.COMPLETED, (
        f"BackgroundJob.status must be JobStatus.COMPLETED, got: {job.status}"
    )

    # Assertion 4: is_admin is False regardless of submitter role
    assert job.is_admin is False, (
        "is_admin must be False for xray_search jobs regardless of submitter role. "
        "This field is a job-priority flag (opt-in via submit_job is_admin=True), "
        "NOT a reflection of the user's role. xray handlers never request the priority lane."
    )
