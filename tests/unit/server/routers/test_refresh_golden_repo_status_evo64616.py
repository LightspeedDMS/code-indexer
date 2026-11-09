"""issue-64616: POST /api/admin/golden-repos/{alias}/refresh answers the cause.

Every failure to submit a refresh used to become a 500 whose cause went only
into the response body; the server log showed a bare ``HTTP 500``. Now:

* a refresh already queued or running (DuplicateJobError) -> 409
* an alias registered but not globally activated -> 404
* maintenance mode -> 503 with Retry-After
* anything else -> 500, logged with its traceback
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.global_repos.refresh_scheduler import (
    GlobalAliasNotFoundError,
    RefreshScheduler,
)
from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.jobs.exceptions import MaintenanceModeError
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.routers.inline_admin_ops import register_admin_ops_routes

_ALIAS = "example-repo"
_URL = f"/api/admin/golden-repos/{_ALIAS}/refresh"
_LOGGER = "code_indexer.server.routers.inline_admin_ops"


def _client(trigger_side_effect: Exception) -> TestClient:
    app = FastAPI()
    grm = MagicMock()
    grm.get_golden_repo.return_value = object()  # registered
    scheduler = MagicMock()
    scheduler.trigger_refresh_for_repo.side_effect = trigger_side_effect
    app.state.global_lifecycle_manager = SimpleNamespace(refresh_scheduler=scheduler)
    register_admin_ops_routes(
        app,
        jwt_manager=MagicMock(),
        user_manager=MagicMock(),
        golden_repo_manager=grm,
        background_job_manager=MagicMock(),
        config_service=MagicMock(),
        server_config=MagicMock(),
        data_dir="/tmp",
        job_tracker=MagicMock(),
    )
    admin = User(
        username="example-admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
    return TestClient(app, raise_server_exceptions=False)


class TestRefreshSubmissionStatus:
    def test_refresh_already_running_is_409(self, caplog) -> None:
        exc = DuplicateJobError("global_repo_refresh", f"{_ALIAS}-global", "job-1")

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            resp = _client(exc).post(_URL)

        assert resp.status_code == 409, resp.text
        assert "job-1" in resp.text
        assert any("job-1" in r.getMessage() for r in caplog.records)

    def test_alias_without_global_activation_is_404(self, caplog) -> None:
        exc = GlobalAliasNotFoundError(
            f"Repository '{_ALIAS}' not found in global registry"
        )

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            resp = _client(exc).post(_URL)

        assert resp.status_code == 404, resp.text
        assert "global activation" in resp.json()["detail"]
        assert any(
            r.levelno == logging.WARNING and _ALIAS in r.getMessage()
            for r in caplog.records
        )

    def test_maintenance_mode_is_503_with_retry_after(self) -> None:
        resp = _client(MaintenanceModeError(retry_after_seconds=42)).post(_URL)

        assert resp.status_code == 503, resp.text
        assert resp.headers["Retry-After"] == "42"

    def test_unexpected_failure_is_500_and_logged_with_traceback(self, caplog) -> None:
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            resp = _client(RuntimeError("backend down")).post(_URL)

        assert resp.status_code == 500
        assert "backend down" in resp.json()["detail"]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "a 500 must name its cause in the server log"
        assert errors[0].exc_info is not None
        assert _ALIAS in errors[0].getMessage()


class TestGlobalAliasNotFoundError:
    def test_unknown_alias_raises_typed_error(self) -> None:
        fake = SimpleNamespace(
            registry=SimpleNamespace(get_global_repo=lambda _name: None)
        )

        with pytest.raises(GlobalAliasNotFoundError):
            RefreshScheduler._resolve_global_alias(cast(RefreshScheduler, fake), _ALIAS)

    def test_typed_error_is_still_a_value_error(self) -> None:
        # trigger_refresh_for_repo documents ValueError; callers keep working.
        assert issubclass(GlobalAliasNotFoundError, ValueError)
