"""
Tests for ClaudeServerClient model parameter (Story #76 - AC6).

Verifies that ClaudeServerClient.create_job includes optional model
parameter in JSON payload when provided.
"""

import pytest
from pytest_httpx import HTTPXMock

# Test constants
TEST_BASE_URL = "https://claude-server.example.com"
TEST_USERNAME = "test_user"
TEST_PASSWORD = "test_password123"


class TestClaudeServerClientModelParameter:
    """Test suite for create_job model parameter."""

    @pytest.mark.asyncio
    async def test_create_job_includes_model_when_provided(self, httpx_mock: HTTPXMock):
        """AC6: create_job includes Model field in JSON when model parameter provided."""
        from code_indexer.server.clients.claude_server_client import (
            ClaudeServerClient,
        )

        # Auth response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
            status_code=200,
        )

        # Create job response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={
                "job_id": "job-12345",
                "status": "created",
            },
            status_code=201,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        await client.create_job(
            prompt="Test prompt", repositories=["repo1"], model="opus"
        )

        # Verify the request included Model field
        requests = httpx_mock.get_requests()
        job_request = [r for r in requests if r.url.path == "/jobs"][0]
        import json

        request_data = json.loads(job_request.content)
        assert "Model" in request_data
        assert request_data["Model"] == "opus"

    @pytest.mark.asyncio
    async def test_create_job_includes_model_sonnet(self, httpx_mock: HTTPXMock):
        """AC6: create_job correctly passes model='sonnet' in JSON."""
        from code_indexer.server.clients.claude_server_client import (
            ClaudeServerClient,
        )

        # Auth response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
            status_code=200,
        )

        # Create job response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={
                "job_id": "job-67890",
                "status": "created",
            },
            status_code=201,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        await client.create_job(
            prompt="Test prompt", repositories=["repo1"], model="sonnet"
        )

        # Verify the request included Model=sonnet
        requests = httpx_mock.get_requests()
        job_request = [r for r in requests if r.url.path == "/jobs"][0]
        import json

        request_data = json.loads(job_request.content)
        assert request_data["Model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_create_job_omits_model_when_not_provided(
        self, httpx_mock: HTTPXMock
    ):
        """AC6: create_job does NOT include Model field when model=None."""
        from code_indexer.server.clients.claude_server_client import (
            ClaudeServerClient,
        )

        # Auth response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
            status_code=200,
        )

        # Create job response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={
                "job_id": "job-99999",
                "status": "created",
            },
            status_code=201,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        # Call without model parameter
        await client.create_job(prompt="Test prompt", repositories=["repo1"])

        # Verify the request does NOT include Model field
        requests = httpx_mock.get_requests()
        job_request = [r for r in requests if r.url.path == "/jobs"][0]
        import json

        request_data = json.loads(job_request.content)
        assert "Model" not in request_data

    @pytest.mark.asyncio
    async def test_create_job_model_parameter_optional(self, httpx_mock: HTTPXMock):
        """AC6: Verify model parameter is optional with default None."""
        from code_indexer.server.clients.claude_server_client import (
            ClaudeServerClient,
        )

        # Auth response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json={"access_token": "token", "token_type": "bearer"},
            status_code=200,
        )

        # Create job response
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={"job_id": "job-00000", "status": "created"},
            status_code=201,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        # Should work without model parameter (no TypeError)
        result = await client.create_job(prompt="Test", repositories=["repo"])

        assert result["job_id"] == "job-00000"
