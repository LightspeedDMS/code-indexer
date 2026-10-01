"""
TDD tests for Bug 6: idempotent POST /api/repos/activate endpoint.

Red phase: these tests FAIL before the fix is applied.

Coverage:
  - Same user + same alias, repo already activated  -> 200, job_id="" (no job to poll)
  - Same user + same alias, activation in-flight    -> 200 with the existing job_id
  - Same user + different alias, same golden         -> 202 (new activation)

job_id="" is the sentinel for "already done, nothing to poll".
job_id=<existing> is returned when a job is still running.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import Mock

import pytest

from code_indexer.server.app import app
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoError
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    admin_client,  # noqa: F401
)

_UNSET = object()


@pytest.fixture(autouse=True)
def _admin_access_service(tmp_path: Path) -> Iterator[None]:
    """Wire a REAL access service on app.state with 'testadmin' in admins.

    Activation requires group access checked by the access service and
    fails closed without it, so these idempotency tests need it wired;
    the admin bypass keeps them focused on idempotent behaviour.
    """
    gam = GroupAccessManager(tmp_path / "groups.db")
    admins = gam.get_group_by_name("admins")
    assert admins is not None, "bootstrap must create the 'admins' group"
    gam.assign_user_to_group("testadmin", admins.id, assigned_by="test")
    previous = getattr(app.state, "access_filtering_service", _UNSET)
    app.state.access_filtering_service = AccessFilteringService(gam)
    try:
        yield
    finally:
        if previous is _UNSET:
            del app.state.access_filtering_service
        else:
            app.state.access_filtering_service = previous


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_activated_repo_manager_already_activated(user_alias: str) -> Mock:
    """Return a mock ActivatedRepoManager that raises 'already activated'."""
    mock_arm = Mock()
    mock_arm.activate_repository.side_effect = ActivatedRepoError(
        f"Repository '{user_alias}' already activated for user 'testadmin'"
    )
    return mock_arm


def _make_activated_repo_manager_in_flight(
    user_alias: str, existing_job_id: str
) -> Mock:
    """Return a mock ActivatedRepoManager that raises DuplicateJobError."""
    mock_arm = Mock()
    mock_arm.activate_repository.side_effect = DuplicateJobError(
        "activate_repository", user_alias, existing_job_id
    )
    return mock_arm


def _make_activated_repo_manager_success(job_id: str) -> Mock:
    """Return a mock ActivatedRepoManager that succeeds with a new job_id."""
    mock_arm = Mock()
    mock_arm.activate_repository.return_value = job_id
    return mock_arm


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestActivateRepositoryIdempotent:
    """POST /api/repos/activate -- idempotent behaviour for same user/alias."""

    # ------------------------------------------------------------------
    # Already activated → 200 with job_id=""
    # ------------------------------------------------------------------

    def test_already_activated_returns_200_with_empty_job_id(self, admin_client):  # noqa: F811
        """Same user + same alias already activated -> 200, job_id="" (not 409).

        job_id="" is the sentinel meaning "activation is already complete;
        there is no pending job to poll".
        """
        handler = _find_route_handler("/api/repos/activate", "POST")
        mock_arm = _make_activated_repo_manager_already_activated("markupsafe")
        mock_grm = Mock()

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "golden_repo_manager", mock_grm):
                response = admin_client.post(
                    "/api/repos/activate",
                    json={
                        "golden_repo_alias": "markupsafe",
                        "user_alias": "markupsafe",
                    },
                )

        assert response.status_code == 200, (
            f"Expected 200 (idempotent already-activated), got {response.status_code}: "
            f"{response.json()}"
        )
        assert response.json().get("job_id") == "", (
            f"Expected job_id='' (already-done sentinel), got: {response.json()}"
        )

    # ------------------------------------------------------------------
    # In-flight job → 200 with existing job_id
    # ------------------------------------------------------------------

    def test_in_flight_job_returns_200_with_existing_job_id(self, admin_client):  # noqa: F811
        """Same user + same alias already activating -> 200, returns existing job_id.

        The caller should wait on the existing job_id rather than starting
        a duplicate activation.
        """
        existing_job_id = "existing-job-abc123"
        handler = _find_route_handler("/api/repos/activate", "POST")
        mock_arm = _make_activated_repo_manager_in_flight("markupsafe", existing_job_id)
        mock_grm = Mock()

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "golden_repo_manager", mock_grm):
                response = admin_client.post(
                    "/api/repos/activate",
                    json={
                        "golden_repo_alias": "markupsafe",
                        "user_alias": "markupsafe",
                    },
                )

        assert response.status_code == 200, (
            f"Expected 200 (idempotent in-flight), got {response.status_code}: "
            f"{response.json()}"
        )
        assert response.json().get("job_id") == existing_job_id, (
            f"Expected existing job_id '{existing_job_id}', got: {response.json()}"
        )

    # ------------------------------------------------------------------
    # Different alias → 202 (new activation)
    # ------------------------------------------------------------------

    def test_different_alias_same_golden_returns_202(self, admin_client):  # noqa: F811
        """Different user_alias for same golden repo -> 202 (new activation, not idempotent)."""
        new_job_id = "new-job-xyz789"
        handler = _find_route_handler("/api/repos/activate", "POST")
        mock_arm = _make_activated_repo_manager_success(new_job_id)
        mock_grm = Mock()

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "golden_repo_manager", mock_grm):
                response = admin_client.post(
                    "/api/repos/activate",
                    json={
                        "golden_repo_alias": "markupsafe",
                        "user_alias": "markupsafe_v2",
                    },
                )

        assert response.status_code == 202, (
            f"Expected 202 (new activation), got {response.status_code}: "
            f"{response.json()}"
        )
        assert response.json().get("job_id") == new_job_id, (
            f"Expected new job_id '{new_job_id}', got: {response.json()}"
        )
