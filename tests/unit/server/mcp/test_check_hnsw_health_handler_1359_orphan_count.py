"""
Story #1359 (Epic #1333, S2) AC4: orphan_count exposed via the check_hnsw_health
MCP tool response.

check_hnsw_health serializes the HNSWHealthService HealthCheckResult via
generic model_dump(), so orphan_count flows through automatically once the
field exists on the model -- this test locks in that behavior explicitly so
a future refactor of the handler (e.g. hand-picking fields instead of
model_dump) cannot silently drop it.
"""

import json
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.services.hnsw_health_service import HealthCheckResult


@pytest.fixture
def mock_regular_user():
    user = Mock(spec=User)
    user.username = "alice"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def broken_health_result():
    return HealthCheckResult(
        valid=False,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=0,
        max_inbound=10,
        orphan_count=4,
        index_path="/path/to/index.bin",
        file_size_bytes=1024000,
        errors=[f"Element {i} has no inbound connections (orphan)" for i in range(4)],
        check_duration_ms=45.5,
        from_cache=False,
    )


class TestCheckHnswHealthHandlerExposesOrphanCount:
    def test_handler_response_includes_orphan_count(
        self, mock_regular_user, broken_health_result
    ):
        from code_indexer.server.mcp.handlers import check_hnsw_health

        params = {"repository_alias": "test-repo", "force_refresh": False}

        mock_repo = Mock()
        mock_repo.clone_path = "/path/to/repo"

        with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
            mock_manager.get_golden_repo = Mock(return_value=mock_repo)

            with patch(
                "code_indexer.server.mcp.handlers._get_hnsw_health_service"
            ) as mock_getter:
                mock_service = Mock()
                mock_service.check_health = Mock(return_value=broken_health_result)
                mock_getter.return_value = mock_service

                result = check_hnsw_health(params, mock_regular_user)

                response_data = json.loads(result["content"][0]["text"])
                assert response_data["success"] is True
                assert response_data["health"]["orphan_count"] == 4
                # Zero-tolerance: orphan_count > 0 must map to valid=False.
                assert response_data["health"]["valid"] is False
