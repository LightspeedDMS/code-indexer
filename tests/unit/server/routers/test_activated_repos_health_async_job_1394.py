"""
Bug #1394: async job endpoint for activated-repo health checks.

GET /api/activated-repos/{user_alias}/health previously ran the full health
check synchronously, building a FRESH cache-less HNSWHealthService() per
request (no 5-minute TTL benefit at all) and iterating collections serially
with no per-collection exception isolation. This suite covers:

- The GET rewire onto the shared compute_repository_health() aggregator
  (Bug #1394 section 3), preserving the existing HealthCheckResponse wire
  shape exactly (user_alias, overall_healthy, status, total_collections,
  healthy_count, unhealthy_count, collections: List[Dict]).
- The new POST /api/activated-repos/{user_alias}/health/check endpoint
  (section 4b), which returns the RepositoryHealthResult shape directly
  (not the legacy HealthCheckResponse wrapper) since nothing depends on the
  job's result shape and this keeps the frontend's renderHealthIndicator/
  renderHealthDetails working unmodified for all three call sites.

activated_repos.py's `_get_activated_repo_manager` / `_get_background_job_manager`
helpers read from the GLOBAL `code_indexer.server.app.app.state` singleton, so
they are patched directly rather than set as attributes on a locally-built
FastAPI test app.

Real BackgroundJobManager (in-memory, no sqlite) -- no mocking of job
submission/execution. Real on-disk hnswlib indexes for aggregation assertions.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import hnswlib
import numpy as np
import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.routers import activated_repos
from code_indexer.server.routers.inline_jobs import register_job_routes
from code_indexer.server.services.repository_health_aggregator import (
    RepositoryHealthResult,
)

DIM = 16
JOB_POLL_TIMEOUT_SEC = 10.0
JOB_POLL_INTERVAL_SEC = 0.05


def _build_real_index(path: Path, num_elements: int = 20) -> None:
    rng = np.random.RandomState(11)
    vectors = rng.randn(num_elements, DIM).astype(np.float32)

    index = hnswlib.Index(space="l2", dim=DIM)
    index.init_index(max_elements=num_elements, ef_construction=100, M=8)
    index.add_items(vectors, np.arange(num_elements))
    index.save_index(str(path))


def _make_activated_repo_manager(repo_path: Path):
    mock_arm = MagicMock()
    mock_arm.get_activated_repo_path.return_value = str(repo_path)
    return mock_arm


def _test_user() -> User:
    return User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def real_job_manager():
    manager = BackgroundJobManager()
    yield manager
    time.sleep(0.1)


class _AppUnderTest:
    def __init__(
        self,
        repo_path: Path,
        job_manager: BackgroundJobManager,
        mount_job_routes: bool = False,
    ):
        self._repo_path = repo_path
        self._job_manager = job_manager
        self._mount_job_routes = mount_job_routes
        self._stack: Optional[ExitStack] = None

    def __enter__(self) -> TestClient:
        app = FastAPI()
        app.include_router(activated_repos.router)
        app.dependency_overrides[get_current_user_hybrid] = _test_user

        if self._mount_job_routes:
            register_job_routes(
                app,
                jwt_manager=None,
                user_manager=None,
                background_job_manager=self._job_manager,
                job_tracker=None,
            )

        mock_arm = _make_activated_repo_manager(self._repo_path)

        self._stack = ExitStack()
        self._stack.enter_context(
            patch.object(
                activated_repos,
                "_get_activated_repo_manager",
                return_value=mock_arm,
            )
        )
        self._stack.enter_context(
            patch.object(
                activated_repos,
                "_get_background_job_manager",
                return_value=self._job_manager,
            )
        )

        return TestClient(app, raise_server_exceptions=False)

    def __exit__(self, *exc_info):
        assert self._stack is not None
        self._stack.close()


def _poll_job_status(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + JOB_POLL_TIMEOUT_SEC
    last_body: Optional[dict] = None
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last_body = resp.json()
        if last_body["status"] in ("completed", "failed", "cancelled"):
            return last_body
        time.sleep(JOB_POLL_INTERVAL_SEC)
    raise AssertionError(f"Job {job_id} did not reach terminal state: {last_body}")


class TestGetHealthEndpointRemoved:
    """Bug #1453: the synchronous GET /{user_alias}/health endpoint is
    REMOVED entirely (it could block a request thread past the reverse-proxy
    timeout on repositories with many temporal shards -- the same hazard
    #1394 fixed for the async POST sibling, which stays live). No route
    should match this path/method combination anymore."""

    def test_get_health_route_no_longer_exists(self, tmp_path: Path, real_job_manager):
        index_dir = tmp_path / ".code-indexer" / "index"
        coll = index_dir / "voyage-code-3"
        coll.mkdir(parents=True)
        _build_real_index(coll / "hnsw_index.bin")

        with _AppUnderTest(tmp_path, real_job_manager) as client:
            response = client.get("/api/activated-repos/myrepo/health")

        assert response.status_code != status.HTTP_200_OK, response.text
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text


class TestPostHealthCheckSubmitsJob:
    def test_returns_202_with_job_id_string(self, tmp_path: Path, real_job_manager):
        with _AppUnderTest(tmp_path, real_job_manager) as client:
            response = client.post("/api/activated-repos/myrepo/health/check")

        assert response.status_code == 202, response.text
        body = response.json()
        assert isinstance(body["job_id"], str)
        assert len(body["job_id"]) > 0

    def test_repo_not_found_returns_404(self, tmp_path: Path, real_job_manager):
        missing_path = tmp_path / "does-not-exist"
        with _AppUnderTest(missing_path, real_job_manager) as client:
            response = client.post("/api/activated-repos/myrepo/health/check")

        assert response.status_code == 404


class TestPostHealthCheckDuplicateJob:
    def test_duplicate_pending_job_returns_409(self, tmp_path: Path, real_job_manager):
        existing_job_id = "existing-activated-health-check-job"
        job = BackgroundJob(
            job_id=existing_job_id,
            operation_type="activated_repo_health_check",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="alice",
            is_admin=False,
            repo_alias="myrepo",
            actor_username="alice",
        )
        real_job_manager.jobs[existing_job_id] = job

        with _AppUnderTest(tmp_path, real_job_manager) as client:
            response = client.post("/api/activated-repos/myrepo/health/check")

        assert response.status_code == 409, response.text


class TestWorkerResultShapeIsRepositoryHealthResult:
    """Section 4b: the async job's result is the canonical RepositoryHealthResult
    shape (NOT the legacy HealthCheckResponse wrapper)."""

    def test_worker_result_dict_has_repository_health_result_fields(
        self, tmp_path: Path, real_job_manager
    ):
        with _AppUnderTest(tmp_path, real_job_manager, mount_job_routes=True) as client:
            response = client.post("/api/activated-repos/myrepo/health/check")
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]

            job_status = _poll_job_status(client, job_id)

        assert job_status["status"] == "completed", job_status

        result = job_status["result"]
        expected_fields = set(RepositoryHealthResult.model_fields.keys())
        assert expected_fields.issubset(set(result.keys()))
        assert result["repo_alias"] == "myrepo"
        assert result["overall_healthy"] is True
        assert result["total_collections"] == 0
        # Must NOT be the legacy HealthCheckResponse shape.
        assert "user_alias" not in result
        assert "status" not in result


class TestFullSubmitPollIntegration:
    """TDD requirement (d): full submit -> poll -> completed -> result flow."""

    def test_full_flow_against_real_hnsw_indexes(
        self, tmp_path: Path, real_job_manager
    ):
        index_dir = tmp_path / ".code-indexer" / "index"
        for name in ["voyage-code-3", "code-indexer-temporal"]:
            coll = index_dir / name
            coll.mkdir(parents=True)
            _build_real_index(coll / "hnsw_index.bin")

        with _AppUnderTest(tmp_path, real_job_manager, mount_job_routes=True) as client:
            submit_resp = client.post(
                "/api/activated-repos/myrepo/health/check",
                params={"force_refresh": "true"},
            )
            assert submit_resp.status_code == 202, submit_resp.text
            job_id = submit_resp.json()["job_id"]

            final_status = _poll_job_status(client, job_id)

        assert final_status["status"] == "completed", final_status

        result = final_status["result"]
        assert result["overall_healthy"] is True
        assert result["total_collections"] == 2
        assert result["healthy_count"] == 2
        assert result["unhealthy_count"] == 0
        names = {c["collection_name"] for c in result["collections"]}
        assert names == {"voyage-code-3", "code-indexer-temporal"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
