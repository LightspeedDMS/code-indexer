"""Unit tests for check_hnsw_health MCP handler (Bug #1453: job-submission conversion).

The old handler ran a bespoke on-disk collection discovery
(iter_index_files_for_repo) plus HNSWHealthService.check_health() inline,
synchronously, inside the MCP sync-dispatch branch -- blocking the request
thread past the generic 60s handler timeout on repos with many collections
(e.g. dozens of temporal shards on a 94GB repo, the production incident that
triggered this bug).

The new handler keeps parameter validation and golden-repo resolution
identical, then submits a background job (operation_type=
"repository_health_check" -- the SAME job type REST's async health endpoints
already use, Bug #1394) via the shared repository_health_aggregator's
compute_repository_health()/get_shared_health_service(), and returns
{success, job_id, message} immediately. Polling happens via the existing
get_job_details/get_job_statistics MCP tools.
"""

import json
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import numpy as np
import pytest

from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    DuplicateJobError,
    JobStatus,
)
from code_indexer.server.services.repository_health_aggregator import (
    RepositoryHealthResult,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index


@pytest.fixture
def mock_regular_user():
    """Create a mock regular user for testing."""
    user = Mock(spec=User)
    user.username = "alice"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _mock_repo(clone_path="/path/to/repo"):
    repo = Mock()
    repo.clone_path = clone_path
    return repo


def _empty_health_result(repo_alias="test-repo"):
    return RepositoryHealthResult(
        repo_alias=repo_alias,
        overall_healthy=True,
        collections=[],
        total_collections=0,
        healthy_count=0,
        unhealthy_count=0,
        from_cache=False,
    )


class TestCheckHnswHealthHandlerRegistration:
    """Test that check_hnsw_health is registered in the MCP tool system."""

    def test_handler_registered_in_handler_registry(self):
        """Test that check_hnsw_health handler is registered in HANDLER_REGISTRY."""
        assert "check_hnsw_health" in HANDLER_REGISTRY
        assert callable(HANDLER_REGISTRY["check_hnsw_health"])


class TestCheckHnswHealthParameterValidation:
    """Missing-parameter / repo-not-found stay purely synchronous errors --
    no background job is ever submitted for these paths."""

    def test_missing_repository_alias_returns_error_without_job(
        self, mock_regular_user
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.background_job_manager = MagicMock()

            result = check_hnsw_health({}, mock_regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is False
        assert "repository_alias" in response["error"]
        mock_app.background_job_manager.submit_job.assert_not_called()

    def test_unknown_repository_returns_error_without_job(self, mock_regular_user):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "nonexistent-repo", "force_refresh": False}

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.golden_repo_manager.get_golden_repo = Mock(return_value=None)
            mock_app.background_job_manager = MagicMock()

            result = check_hnsw_health(params, mock_regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is False
        assert "not found" in response["error"].lower()
        mock_app.background_job_manager.submit_job.assert_not_called()


class TestCheckHnswHealthSubmitsBackgroundJob:
    """Happy path: handler resolves the repo then submits a background job
    and returns immediately -- it never calls compute_repository_health()
    itself (only the uninvoked job closure references it)."""

    def test_returns_job_id_immediately_without_blocking_on_compute(
        self, mock_regular_user
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": False}
        captured = {}

        def fake_submit_job(*, operation_type, func, submitter_username, repo_alias):
            captured["operation_type"] = operation_type
            captured["func"] = func
            captured["submitter_username"] = submitter_username
            captured["repo_alias"] = repo_alias
            return "job-abc-123"

        with (
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.repos.compute_repository_health"
            ) as mock_compute,
        ):
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=_mock_repo()
            )
            mock_app.background_job_manager.submit_job = Mock(
                side_effect=fake_submit_job
            )

            result = check_hnsw_health(params, mock_regular_user)

        # The handler itself must never invoke compute_repository_health --
        # only the (uninvoked) job closure holds a reference to it.
        mock_compute.assert_not_called()

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        assert response["job_id"] == "job-abc-123"
        assert "message" in response

        assert captured["operation_type"] == "repository_health_check"
        assert captured["repo_alias"] == "test-repo"
        assert captured["submitter_username"] == "alice"
        assert callable(captured["func"])

    def test_job_closure_invokes_compute_repository_health_with_correct_args(
        self, mock_regular_user
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": True}
        captured = {}
        fake_result = _empty_health_result()

        def fake_submit_job(*, operation_type, func, submitter_username, repo_alias):
            captured["func"] = func
            return "job-xyz"

        with (
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.repos.compute_repository_health"
            ) as mock_compute,
        ):
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=_mock_repo(clone_path="/home/user/repos/test-repo")
            )
            mock_app.background_job_manager.submit_job = Mock(
                side_effect=fake_submit_job
            )
            mock_compute.return_value = fake_result

            check_hnsw_health(params, mock_regular_user)
            job_result = captured["func"]()

        mock_compute.assert_called_once()
        call_args = mock_compute.call_args
        assert call_args.args[0] == "test-repo"
        assert (
            call_args.args[1]
            == Path("/home/user/repos/test-repo") / ".code-indexer" / "index"
        )
        assert call_args.kwargs["force_refresh"] is True

        assert job_result["repo_alias"] == "test-repo"
        assert job_result["health"] == {}
        assert job_result["collections"] == []

    def test_handles_missing_force_refresh_parameter_defaults_to_false(
        self, mock_regular_user
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo"}  # no force_refresh
        captured = {}
        fake_result = _empty_health_result()

        def fake_submit_job(*, operation_type, func, submitter_username, repo_alias):
            captured["func"] = func
            return "job-1"

        with (
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.repos.compute_repository_health"
            ) as mock_compute,
        ):
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=_mock_repo()
            )
            mock_app.background_job_manager.submit_job = Mock(
                side_effect=fake_submit_job
            )
            mock_compute.return_value = fake_result

            check_hnsw_health(params, mock_regular_user)
            captured["func"]()

        assert mock_compute.call_args.kwargs["force_refresh"] is False


class TestCheckHnswHealthDuplicateJob:
    def test_duplicate_job_returns_error_with_existing_job_id(self, mock_regular_user):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo"}

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=_mock_repo()
            )
            mock_app.background_job_manager.submit_job = Mock(
                side_effect=DuplicateJobError(
                    "repository_health_check", "test-repo", "existing-job-99"
                )
            )

            result = check_hnsw_health(params, mock_regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is False
        assert response["existing_job_id"] == "existing-job-99"


class TestCheckHnswHealthBackgroundJobManagerUnavailable:
    def test_returns_error_when_background_job_manager_is_none(self, mock_regular_user):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo"}

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=_mock_repo()
            )
            mock_app.background_job_manager = None

            result = check_hnsw_health(params, mock_regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is False
        assert "background job manager" in response["error"].lower()


class TestCheckHnswHealthRealBackgroundJobManagerEndToEnd:
    """At least one test per surface must go through a REAL BackgroundJobManager
    submit -> execute -> poll flow with a real on-disk HNSW index (no mocking
    of BackgroundJobManager itself, per this codebase's testing philosophy)."""

    def test_submit_execute_poll_against_real_hnsw_index(
        self, tmp_path, mock_regular_user
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        clone_path = tmp_path / "repo"
        coll = clone_path / ".code-indexer" / "index" / "voyage-code-3"
        coll.mkdir(parents=True)
        vectors = np.random.RandomState(3).randn(50, 32).astype(np.float32)
        index = build_hnsw_index(vectors, num_threads=1)
        index.save_index(str(coll / "hnsw_index.bin"))

        real_job_manager = BackgroundJobManager()

        params = {"repository_alias": "test-repo", "force_refresh": True}

        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=_mock_repo(clone_path=str(clone_path))
            )
            mock_app.background_job_manager = real_job_manager

            result = check_hnsw_health(params, mock_regular_user)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        job_id = response["job_id"]

        deadline = time.time() + 10.0
        job = None
        while time.time() < deadline:
            job = real_job_manager.jobs.get(job_id)
            if job is not None and job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
            ):
                break
            time.sleep(0.05)

        assert job is not None
        assert job.status == JobStatus.COMPLETED, job.error
        assert job.result["repo_alias"] == "test-repo"
        assert job.result["overall_healthy"] is True
        assert job.result["total_collections"] == 1
        assert job.result["health"]["collection_name"] == "voyage-code-3"
        assert job.result["collections"][0]["collection_name"] == "voyage-code-3"
