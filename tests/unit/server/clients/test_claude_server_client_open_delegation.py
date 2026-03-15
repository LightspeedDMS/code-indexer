"""
Unit tests for ClaudeServerClient open delegation methods.

Story #456: Open-ended delegation with engine and mode selection

Tests follow TDD methodology - tests written FIRST before implementation.
Uses httpx_mock for HTTP mocking per project conventions (not mocks of objects).

Tests cover:
- create_job_with_options: builds correct request payload with engine/model/timeout
- wait_for_repo_ready: repo already ready, not registered, timeout, failed status
- get_repo_status: returns correct data, handles 404
- Backward compatibility: existing create_job() method signature unchanged
"""

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


class TestCreateJobWithOptions:
    """Tests for create_job_with_options() method."""

    @pytest.mark.asyncio
    async def test_create_job_with_options_sends_correct_payload(
        self, httpx_mock: HTTPXMock
    ):
        """
        create_job_with_options sends correct JSON to POST /jobs.

        Given engine, model, timeout, and repositories
        When create_job_with_options is called
        Then it sends the correct request body with Options nested object
        """
        import json

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={"jobId": "job-abc123", "status": "created"},
            status_code=201,
        )

        client = make_client()
        result = await client.create_job_with_options(
            prompt="Fix the bug",
            repositories=["main-app", "utils-lib"],
            engine="claude-code",
            model="claude-opus-4-5",
            timeout=3600,
        )

        assert result["jobId"] == "job-abc123"

        # Verify the request body was correct
        requests = httpx_mock.get_requests()
        job_request = next(
            r for r in requests if "/jobs" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert body["Prompt"] == "Fix the bug"
        assert body["Repositories"] == ["main-app", "utils-lib"]
        assert "Options" in body
        assert body["Options"]["agentEngine"] == "claude-code"
        assert body["Options"]["model"] == "claude-opus-4-5"
        assert body["Options"]["timeout"] == 3600

    @pytest.mark.asyncio
    async def test_create_job_with_options_engine_only(self, httpx_mock: HTTPXMock):
        """
        create_job_with_options works with only engine specified (model/timeout optional).

        Given only engine is specified (no model or timeout)
        When create_job_with_options is called
        Then it sends Options with agentEngine but no model or timeout keys
        """
        import json

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={"jobId": "job-xyz789", "status": "created"},
            status_code=201,
        )

        client = make_client()
        result = await client.create_job_with_options(
            prompt="Analyze code",
            repositories=["main-app"],
            engine="codex",
        )

        assert result["jobId"] == "job-xyz789"

        requests = httpx_mock.get_requests()
        job_request = next(
            r for r in requests if "/jobs" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert body["Options"]["agentEngine"] == "codex"
        # model and timeout should not be present when not specified
        assert "model" not in body["Options"] or body["Options"]["model"] is None
        assert "timeout" not in body["Options"] or body["Options"]["timeout"] is None

    @pytest.mark.asyncio
    async def test_create_job_with_options_returns_job_id(self, httpx_mock: HTTPXMock):
        """create_job_with_options returns the full response dict from Claude Server."""
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={
                "jobId": "job-full-response",
                "status": "created",
                "user": "testuser",
            },
            status_code=201,
        )

        client = make_client()
        result = await client.create_job_with_options(
            prompt="Do something",
            repositories=["repo1"],
            engine="gemini",
        )

        assert result["jobId"] == "job-full-response"
        assert result["status"] == "created"

    @pytest.mark.asyncio
    async def test_create_job_with_options_raises_on_server_error(
        self, httpx_mock: HTTPXMock
    ):
        """create_job_with_options raises ClaudeServerError on 5xx response."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerError

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={"detail": "Internal server error"},
            status_code=500,
        )

        client = make_client()
        with pytest.raises(ClaudeServerError):
            await client.create_job_with_options(
                prompt="Do something",
                repositories=["repo1"],
                engine="claude-code",
            )

    @pytest.mark.asyncio
    async def test_create_job_with_options_mcp_servers_included(
        self, httpx_mock: HTTPXMock
    ):
        """
        create_job_with_options includes mcp_servers in Options when provided.
        """
        import json

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={"jobId": "job-mcp", "status": "created"},
            status_code=201,
        )

        client = make_client()
        await client.create_job_with_options(
            prompt="Do something",
            repositories=["repo1"],
            engine="claude-code",
            mcp_servers=["cidx-server", "github-server"],
        )

        requests = httpx_mock.get_requests()
        job_request = next(
            r for r in requests if "/jobs" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        assert body["Options"]["mcpServers"] == ["cidx-server", "github-server"]


class TestBackwardCompatibility:
    """Tests ensuring existing create_job() is completely unchanged."""

    @pytest.mark.asyncio
    async def test_existing_create_job_still_works(self, httpx_mock: HTTPXMock):
        """
        Backward compatibility: existing create_job() method signature unchanged.

        Given a call to the existing create_job() method
        When called with the original parameters
        Then it works exactly as before
        """
        import json

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/jobs",
            json={"jobId": "old-job-123", "status": "created"},
            status_code=201,
        )

        client = make_client()
        result = await client.create_job(
            prompt="Old prompt",
            repositories=["old-repo"],
        )

        assert result["jobId"] == "old-job-123"

        # Verify old format is still sent (no Options wrapping)
        requests = httpx_mock.get_requests()
        job_request = next(
            r for r in requests if "/jobs" in str(r.url) and r.method == "POST"
        )
        body = json.loads(job_request.content)

        # Old method uses lowercase "prompt" and "Repositories"
        assert "prompt" in body or "Prompt" in body
        assert "Repositories" in body

    def test_create_job_method_exists_with_original_signature(self):
        """create_job() method still exists and has original signature."""
        import inspect
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        sig = inspect.signature(ClaudeServerClient.create_job)
        params = list(sig.parameters.keys())

        assert "prompt" in params
        assert "repositories" in params
        # model is optional
        assert "model" in params

    def test_create_job_with_options_method_exists(self):
        """create_job_with_options() method exists as a separate method."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        assert hasattr(ClaudeServerClient, "create_job_with_options")
        assert callable(getattr(ClaudeServerClient, "create_job_with_options"))


class TestGetRepoStatus:
    """Tests for get_repo_status() method."""

    @pytest.mark.asyncio
    async def test_get_repo_status_returns_data_when_found(self, httpx_mock: HTTPXMock):
        """
        get_repo_status returns correct data for a registered repository.

        Given a registered repository
        When get_repo_status is called
        Then it returns the repository data including cloneStatus
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/my-repo",
            json={
                "name": "my-repo",
                "cloneStatus": "success",
                "gitUrl": "https://github.com/org/repo",
            },
            status_code=200,
        )

        client = make_client()
        result = await client.get_repo_status("my-repo")

        assert result["name"] == "my-repo"
        assert result["cloneStatus"] == "success"

    @pytest.mark.asyncio
    async def test_get_repo_status_raises_not_found_on_404(self, httpx_mock: HTTPXMock):
        """
        get_repo_status raises ClaudeServerNotFoundError when repo not registered.

        Given a repository not registered on Claude Server
        When get_repo_status is called
        Then ClaudeServerNotFoundError is raised
        """
        from code_indexer.server.clients.claude_server_client import (
            ClaudeServerNotFoundError,
        )

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/unknown-repo",
            json={"detail": "Not found"},
            status_code=404,
        )

        client = make_client()
        with pytest.raises(ClaudeServerNotFoundError):
            await client.get_repo_status("unknown-repo")

    @pytest.mark.asyncio
    async def test_get_repo_status_raises_on_server_error(self, httpx_mock: HTTPXMock):
        """get_repo_status raises ClaudeServerError on 5xx response."""
        from code_indexer.server.clients.claude_server_client import ClaudeServerError

        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/some-repo",
            json={"detail": "Server error"},
            status_code=500,
        )

        client = make_client()
        with pytest.raises(ClaudeServerError):
            await client.get_repo_status("some-repo")


class TestWaitForRepoReady:
    """Tests for wait_for_repo_ready() method."""

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_returns_true_when_already_ready(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready returns True when repo already registered with cloneStatus=completed.

        Given a repository already registered and cloneStatus="completed" (production value)
        When wait_for_repo_ready is called
        Then it returns True immediately without registering
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/ready-repo",
            json={"name": "ready-repo", "cloneStatus": "completed"},
            status_code=200,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="ready-repo",
            timeout=10,
        )

        assert result is True

        # Verify no registration attempt was made
        requests = httpx_mock.get_requests()
        register_requests = [
            r for r in requests if "/repositories/register" in str(r.url)
        ]
        assert len(register_requests) == 0

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_accepts_success_for_backward_compat(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready accepts cloneStatus="success" for backward compatibility.

        Given a repository already registered and cloneStatus="success" (older server value)
        When wait_for_repo_ready is called
        Then it returns True immediately (backward compatible)
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/legacy-repo",
            json={"name": "legacy-repo", "cloneStatus": "success"},
            status_code=200,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="legacy-repo",
            timeout=10,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_registers_when_not_found(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready registers repo when not found, then polls until ready.

        Given a repository not registered on Claude Server
        When wait_for_repo_ready is called with git_url
        Then it registers the repo via POST /repositories/register
        And polls until cloneStatus="success"
        And returns True
        """
        add_auth_mock(httpx_mock)
        # First GET: 404 (not found)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/new-repo",
            json={"detail": "Not found"},
            status_code=404,
        )
        # POST /repositories/register: returns 201 with cloneStatus=cloning
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/repositories/register",
            json={
                "name": "new-repo",
                "cloneStatus": "cloning",
                "gitUrl": "https://github.com/org/new-repo",
            },
            status_code=201,
        )
        # GET poll: cloneStatus=completed (production value)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/new-repo",
            json={"name": "new-repo", "cloneStatus": "completed"},
            status_code=200,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="new-repo",
            timeout=10,
            git_url="https://github.com/org/new-repo",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_returns_false_on_failed_status(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready returns False when cloneStatus="failed".

        Given a repository with cloneStatus="failed"
        When wait_for_repo_ready is called
        Then it returns False immediately
        """
        add_auth_mock(httpx_mock)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/failed-repo",
            json={"name": "failed-repo", "cloneStatus": "failed"},
            status_code=200,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="failed-repo",
            timeout=10,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_times_out_during_cloning(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready returns False when timeout expires during cloning.

        Given a repository stuck in cloneStatus="cloning"
        When wait_for_repo_ready is called with a very short timeout
        Then it returns False after timeout
        """
        # Allow unused mocked responses since we don't know exactly how many
        # polls will occur before the short timeout expires.
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json={"access_token": "test-token", "token_type": "bearer"},
            status_code=200,
            is_reusable=True,
        )
        # Repo exists but is still cloning
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/slow-repo",
            json={"name": "slow-repo", "cloneStatus": "cloning"},
            status_code=200,
            is_reusable=True,
        )

        client = make_client()
        # Very short timeout to force timeout path
        result = await client.wait_for_repo_ready(
            alias="slow-repo",
            timeout=0.1,
            poll_interval=0.05,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_polls_cloning_until_success(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready polls while cloneStatus="cloning" then returns True when done.

        Given a repo that starts cloning and becomes ready
        When wait_for_repo_ready is called
        Then it polls until cloneStatus="success" and returns True
        """
        add_auth_mock(httpx_mock)
        # First GET: cloning
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/cloning-repo",
            json={"name": "cloning-repo", "cloneStatus": "cloning"},
            status_code=200,
        )
        # Second GET: completed (production value)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/cloning-repo",
            json={"name": "cloning-repo", "cloneStatus": "completed"},
            status_code=200,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="cloning-repo",
            timeout=10,
            poll_interval=0.01,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_no_git_url_on_404_times_out(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready with git_url=None on 404 enters polling loop and times out.

        Given a repository not found on Claude Server (404)
        And git_url is None (no URL to register with)
        When wait_for_repo_ready is called
        Then it does NOT call POST /repositories/register
        And it polls until timeout expires
        And returns False
        """
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json={"access_token": "test-token", "token_type": "bearer"},
            status_code=200,
            is_reusable=True,
        )
        # Repo returns 404 on every poll (never registers)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/missing-repo",
            json={"detail": "Not found"},
            status_code=404,
            is_reusable=True,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="missing-repo",
            timeout=0.1,
            git_url=None,
            poll_interval=0.05,
        )

        assert result is False

        # Verify no registration attempt was made
        requests = httpx_mock.get_requests()
        register_requests = [
            r for r in requests if "/repositories/register" in str(r.url)
        ]
        assert len(register_requests) == 0

    @pytest.mark.asyncio
    async def test_wait_for_repo_ready_409_conflict_means_already_exists(
        self, httpx_mock: HTTPXMock
    ):
        """
        wait_for_repo_ready handles 409 conflict (repo already exists) gracefully.

        Given a 404 on first check, then 409 on register (race condition)
        When wait_for_repo_ready is called
        Then it polls for status and returns True when ready
        """
        add_auth_mock(httpx_mock)
        # First GET: 404 (not found)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/race-repo",
            json={"detail": "Not found"},
            status_code=404,
        )
        # POST /repositories/register: 409 (already exists - race condition)
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/repositories/register",
            json={"detail": "Already exists"},
            status_code=409,
        )
        # GET poll after 409: completed (someone else registered it, production value)
        httpx_mock.add_response(
            method="GET",
            url=f"{TEST_BASE_URL}/repositories/race-repo",
            json={"name": "race-repo", "cloneStatus": "completed"},
            status_code=200,
        )

        client = make_client()
        result = await client.wait_for_repo_ready(
            alias="race-repo",
            timeout=10,
            git_url="https://github.com/org/race-repo",
        )

        assert result is True
