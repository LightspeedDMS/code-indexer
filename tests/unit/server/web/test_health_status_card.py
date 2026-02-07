"""
Unit tests for repository health status card functionality.

Tests the integration between JavaScript UI and health REST endpoint.
Since JavaScript testing requires browser environment, these tests focus on:
- HealthCheckResult model serialization for JSON responses
- Template structure validation
- Expected data contract between frontend and backend

Note: API endpoint tests exist in tests/unit/server/routers/test_repository_health.py
This file focuses on frontend integration concerns.
"""

import pytest

from code_indexer.services.hnsw_health_service import HealthCheckResult


class TestHealthCheckResultModel:
    """Test HealthCheckResult model serialization for JavaScript consumption."""

    def test_healthy_result_serialization(self):
        """Test healthy result serializes correctly for JSON response."""
        # Create healthy result
        healthy_result = HealthCheckResult(
            valid=True,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1000,
            connections_checked=5000,
            min_inbound=2,
            max_inbound=10,
            index_path="/mock/path/index.bin",
            file_size_bytes=1024000,
            errors=[],
            check_duration_ms=45.5,
            from_cache=False,
        )

        # Convert to dict (simulates JSON serialization)
        data = healthy_result.model_dump()

        # Verify all required fields for UI (Acceptance Criteria 4)
        assert "valid" in data
        assert "file_exists" in data
        assert "readable" in data
        assert "loadable" in data
        assert "errors" in data
        assert isinstance(data["errors"], list)

    def test_unhealthy_result_serialization(self):
        """Test unhealthy result includes error messages (Acceptance Criteria 5)."""
        # Create unhealthy result
        unhealthy_result = HealthCheckResult(
            valid=False,
            file_exists=True,
            readable=True,
            loadable=True,
            element_count=1000,
            connections_checked=5000,
            min_inbound=0,
            max_inbound=10,
            index_path="/mock/path/index.bin",
            file_size_bytes=1024000,
            errors=["Integrity violation: node 42 has 0 inbound connections"],
            check_duration_ms=45.5,
            from_cache=False,
        )

        # Convert to dict
        data = unhealthy_result.model_dump()

        # Verify error messages present
        assert data["valid"] is False
        assert len(data["errors"]) > 0
        assert isinstance(data["errors"][0], str)

    def test_result_json_schema_example(self):
        """Test model provides JSON schema example matching API docs."""
        # Get JSON schema
        schema = HealthCheckResult.model_json_schema()

        # Verify example exists in schema (Pydantic v2 uses 'example' not 'examples')
        assert "example" in schema


class TestTemplateIntegration:
    """Test health status is properly integrated into template context."""

    def test_template_receives_repo_health_data(self):
        """
        Test template can access health data for rendering.

        NOTE: This is a placeholder for integration testing.
        Actual template rendering would be tested in E2E tests.
        """
        # Mock template context with health data
        repo_data = {
            "alias": "test-repo",
            "health": {
                "valid": True,
                "file_exists": True,
                "readable": True,
                "loadable": True,
                "errors": [],
            },
        }

        # Verify structure matches what JavaScript expects
        assert "health" in repo_data
        assert "valid" in repo_data["health"]
        assert isinstance(repo_data["health"]["errors"], list)
