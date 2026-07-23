"""
Story #1359 (Epic #1333, S2) AC4: orphan_count survives the check_hnsw_health
MCP job-result mapping (Bug #1453).

Bug #1453 converted check_hnsw_health from an inline synchronous handler into
a background-job submitter. The job closure maps a RepositoryHealthResult
(from compute_repository_health) onto {health, collections, ...} -- this
test locks in that orphan_count is not dropped by that mapping, in both the
top-level "health" convenience field (mirrors collections[0]) and the
"collections" list itself. This test does NOT re-derive orphan_count from
HNSWHealthService internals -- that propagation is already covered by
tests/unit/services/test_hnsw_health_service_1359_orphan_count.py and
tests/unit/server/services/test_repository_health_aggregator_1394.py.
"""

from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.repository_health_aggregator import (
    CollectionHealthResult,
    RepositoryHealthResult,
)


@pytest.fixture
def mock_regular_user():
    user = Mock(spec=User)
    user.username = "alice"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _broken_repository_health_result() -> RepositoryHealthResult:
    broken_collection = CollectionHealthResult(
        collection_name="voyage-code-3",
        index_type="semantic",
        valid=False,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=0,
        max_inbound=10,
        orphan_count=4,
        file_size_bytes=1024000,
        errors=[f"Element {i} has no inbound connections (orphan)" for i in range(4)],
        check_duration_ms=45.5,
    )
    return RepositoryHealthResult(
        repo_alias="test-repo",
        overall_healthy=False,
        collections=[broken_collection],
        total_collections=1,
        healthy_count=0,
        unhealthy_count=1,
        from_cache=False,
    )


class TestCheckHnswHealthJobResultExposesOrphanCount:
    def test_job_closure_result_includes_orphan_count_in_health_and_collections(
        self, mock_regular_user
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": False}
        captured = {}

        def fake_submit_job(*, operation_type, func, submitter_username, repo_alias):
            captured["func"] = func
            return "job-orphan-1"

        with (
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.repos.compute_repository_health"
            ) as mock_compute,
        ):
            mock_app.golden_repo_manager.get_golden_repo = Mock(
                return_value=Mock(clone_path="/path/to/repo")
            )
            mock_app.background_job_manager.submit_job = Mock(
                side_effect=fake_submit_job
            )
            mock_compute.return_value = _broken_repository_health_result()

            check_hnsw_health(params, mock_regular_user)
            job_result = captured["func"]()

        assert job_result["health"]["orphan_count"] == 4
        # Zero-tolerance: orphan_count > 0 must map to valid=False.
        assert job_result["health"]["valid"] is False

        assert job_result["collections"][0]["orphan_count"] == 4
        assert job_result["collections"][0]["valid"] is False
