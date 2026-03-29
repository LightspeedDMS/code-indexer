"""
Unit tests for ClaudeServerClient collaborative and competitive delegation methods.

Story #462: Enable collaborative and competitive delegation modes.

Tests cover:
- create_orchestrated_job: builds correct Steps payload, omits optional None fields
- create_orchestrated_job: handles success (200/201) and error responses
- create_competitive_job: builds correct payload with required and optional fields
- create_competitive_job: handles success (200/201) and error responses
"""

import json

import pytest
from pytest_httpx import HTTPXMock

# Test constants
TEST_BASE_URL = "https://claude-server.example.com"
TEST_USERNAME = "test_user"
TEST_PASSWORD = "test_password123"


def make_client():
    """Create a ClaudeServerClient for testing."""
    from code_indexer.server.clients.claude_server_client import ClaudeServerClient

    return ClaudeServerClient(
        base_url=TEST_BASE_URL,
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
    )


def add_auth_mock(httpx_mock: HTTPXMock):
    """Add authentication mock response."""
    httpx_mock.add_response(
        method="POST",
        url=f"{TEST_BASE_URL}/auth/login",
        json={"access_token": "test-token", "token_type": "bearer"},
        status_code=200,
    )


class TestCreateOrchestratedJob:
    """Tests for create_orchestrated_job() method."""

    @pytest.mark.asyncio
    async def test_sends_correct_payload_with_required_fields(
        self, httpx_mock: HTTPXMock
    ):
        """
        create_orchestrated_job sends correct JSON to POST /jobs/orchestrated.

        Given steps with step_id, engine, prompt
        When create_orchestrated_job is called
        Then it sends Steps array with stepId, engine, prompt, dependsOn fields
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/orchestrated",
            json={"jobId": "orch-job-001", "status": "created"},
            status_code=201,
        )

        client = make_client()
        steps = [
            {
                "step_id": "analyze",
                "engine": "claude-code",
                "prompt": "Analyze the codebase",
            },
            {
                "step_id": "implement",
                "engine": "codex",
                "prompt": "Implement changes",
                "depends_on": ["analyze"],
            },
        ]
        result = await client.create_orchestrated_job(steps)

        assert result["jobId"] == "orch-job-001"

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/orchestrated" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert "Steps" in body
        assert len(body["Steps"]) == 2
        assert body["Steps"][0]["stepId"] == "analyze"
        assert body["Steps"][0]["engine"] == "claude-code"
        assert body["Steps"][0]["prompt"] == "Analyze the codebase"
        assert body["Steps"][0]["dependsOn"] == []
        assert body["Steps"][1]["stepId"] == "implement"
        assert body["Steps"][1]["dependsOn"] == ["analyze"]

    @pytest.mark.asyncio
    async def test_includes_optional_fields_when_provided(self, httpx_mock: HTTPXMock):
        """
        create_orchestrated_job includes repository, repositories, timeoutSeconds,
        options when provided in step dict.
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/orchestrated",
            json={"jobId": "orch-job-002", "status": "created"},
            status_code=201,
        )

        client = make_client()
        steps = [
            {
                "step_id": "step1",
                "engine": "claude-code",
                "prompt": "Do work",
                "repository": "main-app",
                "repositories": ["main-app", "lib"],
                "timeout_seconds": 600,
                "options": {"model": "claude-opus-4-5"},
            },
        ]
        result = await client.create_orchestrated_job(steps)
        assert result["jobId"] == "orch-job-002"

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/orchestrated" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        step = body["Steps"][0]
        assert step["repository"] == "main-app"
        assert step["repositories"] == ["main-app", "lib"]
        assert step["timeoutSeconds"] == 600
        assert step["options"] == {"model": "claude-opus-4-5"}

    @pytest.mark.asyncio
    async def test_omits_optional_fields_when_not_provided(self, httpx_mock: HTTPXMock):
        """
        create_orchestrated_job omits repository, repositories, timeoutSeconds,
        options when not provided (not None, not zero).
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/orchestrated",
            json={"jobId": "orch-job-003", "status": "created"},
            status_code=201,
        )

        client = make_client()
        steps = [
            {
                "step_id": "only-required",
                "engine": "gemini",
                "prompt": "Just required fields",
            },
        ]
        await client.create_orchestrated_job(steps)

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/orchestrated" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        step = body["Steps"][0]
        assert "repository" not in step
        assert "repositories" not in step
        assert "timeoutSeconds" not in step
        assert "options" not in step

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self, httpx_mock: HTTPXMock):
        """create_orchestrated_job raises ClaudeServerError on 500."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerError

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/orchestrated",
            status_code=500,
        )

        client = make_client()
        with pytest.raises(ClaudeServerError, match="500"):
            await client.create_orchestrated_job(
                [{"step_id": "s1", "engine": "claude-code", "prompt": "p"}]
            )

    @pytest.mark.asyncio
    async def test_raises_on_client_error(self, httpx_mock: HTTPXMock):
        """create_orchestrated_job raises ClaudeServerError on 400."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerError

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/orchestrated",
            status_code=400,
        )

        client = make_client()
        with pytest.raises(ClaudeServerError, match="400"):
            await client.create_orchestrated_job(
                [{"step_id": "s1", "engine": "claude-code", "prompt": "p"}]
            )

    @pytest.mark.asyncio
    async def test_timeout_seconds_zero_is_omitted(self, httpx_mock: HTTPXMock):
        """timeout_seconds=0 is treated as not provided and omitted."""
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/orchestrated",
            json={"jobId": "orch-004", "status": "created"},
            status_code=201,
        )

        client = make_client()
        steps = [
            {
                "step_id": "s1",
                "engine": "claude-code",
                "prompt": "p",
                "timeout_seconds": 0,
            },
        ]
        await client.create_orchestrated_job(steps)

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/orchestrated" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)
        assert "timeoutSeconds" not in body["Steps"][0]


class TestCreateCompetitiveJob:
    """Tests for create_competitive_job() method."""

    @pytest.mark.asyncio
    async def test_sends_correct_payload_required_fields(self, httpx_mock: HTTPXMock):
        """
        create_competitive_job sends correct JSON to POST /jobs/competitive
        with required fields only.
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/competitive",
            json={"jobId": "comp-job-001", "status": "created"},
            status_code=201,
        )

        client = make_client()
        result = await client.create_competitive_job(
            prompt="Fix the bug",
            repositories=["main-app"],
            engines=["claude-code", "codex"],
        )

        assert result["jobId"] == "comp-job-001"

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/competitive" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert body["Prompt"] == "Fix the bug"
        assert body["Repositories"] == ["main-app"]
        assert body["Engines"] == ["claude-code", "codex"]

    @pytest.mark.asyncio
    async def test_includes_all_optional_fields(self, httpx_mock: HTTPXMock):
        """
        create_competitive_job includes all optional fields when provided.
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/competitive",
            json={"jobId": "comp-job-002", "status": "created"},
            status_code=201,
        )

        client = make_client()
        result = await client.create_competitive_job(
            prompt="Implement feature",
            repositories=["app", "lib"],
            engines=["claude-code", "codex", "gemini"],
            distribution_strategy="round-robin",
            min_success_threshold=2,
            approach_count=5,
            approach_timeout_seconds=1800,
            decomposer={"engine": "claude-code"},
            judge={"engine": "claude-code", "model": "claude-opus-4-5"},
            options={"verbose": True},
        )

        assert result["jobId"] == "comp-job-002"

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/competitive" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert body["Prompt"] == "Implement feature"
        assert body["Repositories"] == ["app", "lib"]
        assert body["Engines"] == ["claude-code", "codex", "gemini"]
        assert body["DistributionStrategy"] == "round-robin"
        assert body["MinSuccessThreshold"] == 2
        assert body["ApproachCount"] == 5
        assert body["ApproachTimeoutSeconds"] == 1800
        assert body["Decomposer"] == {"engine": "claude-code"}
        assert body["Judge"] == {"engine": "claude-code", "model": "claude-opus-4-5"}
        assert body["Options"] == {"verbose": True}

    @pytest.mark.asyncio
    async def test_omits_optional_fields_when_none(self, httpx_mock: HTTPXMock):
        """
        create_competitive_job omits optional fields when they are None.
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/competitive",
            json={"jobId": "comp-job-003", "status": "created"},
            status_code=201,
        )

        client = make_client()
        await client.create_competitive_job(
            prompt="Task",
            repositories=["repo"],
            engines=["claude-code"],
        )

        requests = httpx_mock.get_requests()
        job_request = next(
            r
            for r in requests
            if "/jobs/competitive" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert "DistributionStrategy" not in body
        assert "MinSuccessThreshold" not in body
        assert "ApproachCount" not in body
        assert "ApproachTimeoutSeconds" not in body
        assert "Decomposer" not in body
        assert "Judge" not in body
        assert "Options" not in body

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self, httpx_mock: HTTPXMock):
        """create_competitive_job raises ClaudeServerError on 500."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerError

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/competitive",
            status_code=500,
        )

        client = make_client()
        with pytest.raises(ClaudeServerError, match="500"):
            await client.create_competitive_job(
                prompt="p",
                repositories=["r"],
                engines=["claude-code"],
            )

    @pytest.mark.asyncio
    async def test_raises_on_client_error(self, httpx_mock: HTTPXMock):
        """create_competitive_job raises ClaudeServerError on 422."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerError

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs/competitive",
            status_code=422,
        )

        client = make_client()
        with pytest.raises(ClaudeServerError, match="422"):
            await client.create_competitive_job(
                prompt="p",
                repositories=["r"],
                engines=["claude-code"],
            )
