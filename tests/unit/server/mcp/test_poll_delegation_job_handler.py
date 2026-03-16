"""
Unit tests for poll_delegation_job MCP tool handler.

Story #720: Poll Delegation Job with Progress Feedback

Tests follow TDD methodology - tests written FIRST before implementation.
"""

import json
from datetime import datetime, timezone

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def test_user():
    """Create a test user."""
    return User(
        username="testuser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_delegation_config():
    """Create mock delegation config."""
    from code_indexer.server.config.delegation_config import ClaudeDelegationConfig

    return ClaudeDelegationConfig(
        function_repo_alias="test-repo",
        claude_server_url="https://claude-server.example.com",
        claude_server_username="service_user",
        claude_server_credential="service_pass",
    )


class TestPollDelegationJobHandler:
    """
    Tests for poll_delegation_job handler basic validation.

    Note: Story #720 changed poll_delegation_job from polling Claude Server
    to callback-based completion. See TestPollDelegationJobCallbackBased for
    tests of the callback-based behavior.

    Story #50: Handler remains async (justified exception) because
    DelegationJobTracker uses asyncio.Future for callback-based completion.
    """

    @pytest.mark.asyncio
    async def test_poll_returns_error_when_not_configured(self, test_user):
        """
        poll_delegation_job returns error when delegation not configured.

        Given delegation is not configured
        When poll_delegation_job is called
        Then it returns error indicating not configured
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: None,
            )

            response = await handle_poll_delegation_job(
                {"job_id": "job-12345"},
                test_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert "not configured" in data["error"].lower()


class TestPollDelegationJobCallbackBased:
    """Tests for non-blocking callback-based job completion (Story #720)."""

    @pytest.fixture(autouse=True)
    def reset_tracker_singleton(self):
        """Reset DelegationJobTracker singleton between tests."""
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
        )

        DelegationJobTracker._instance = None
        yield
        DelegationJobTracker._instance = None

    @pytest.mark.asyncio
    async def test_poll_returns_result_when_job_already_complete(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job returns result immediately when job is already complete.

        Given a job is registered and completed before poll is called
        When poll_delegation_job is called (non-blocking)
        Then it returns the result immediately without waiting
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
            JobResult,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("callback-job-123")

        result = JobResult(
            job_id="callback-job-123",
            status="completed",
            output="The authentication uses OAuth2 with JWT tokens.",
            exit_code=0,
            error=None,
        )
        await tracker.complete_job(result)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_poll_delegation_job(
                {"job_id": "callback-job-123"},
                test_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["status"] == "completed"
        assert "OAuth2" in data["result"]
        assert data["continue_polling"] is False

    @pytest.mark.asyncio
    async def test_poll_returns_failed_result_from_callback(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job returns failed result when job failed.

        Given a job is registered and completed with failure
        When poll_delegation_job is called
        Then it returns the error from the callback
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
            JobResult,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("failed-job-456")

        result = JobResult(
            job_id="failed-job-456",
            status="failed",
            output="Repository clone failed: authentication denied",
            exit_code=1,
            error="Repository clone failed: authentication denied",
        )
        await tracker.complete_job(result)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_poll_delegation_job(
                {"job_id": "failed-job-456"},
                test_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["status"] == "failed"
        assert "clone failed" in data["error"]
        assert data["continue_polling"] is False

    @pytest.mark.asyncio
    async def test_poll_returns_waiting_when_job_not_yet_complete(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job returns waiting status when job is still running.

        Given a job is registered but not yet complete
        When poll_delegation_job is called (non-blocking)
        Then it returns status=waiting with continue_polling=True immediately
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("pending-job-789")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_poll_delegation_job(
                {"job_id": "pending-job-789"},
                test_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["status"] == "waiting"
        assert "still running" in data["message"].lower()
        assert data["continue_polling"] is True

        # Job should still exist in tracker (caller can poll again)
        assert await tracker.has_job("pending-job-789") is True

    @pytest.mark.asyncio
    async def test_can_poll_again_after_waiting_and_get_result(
        self, test_user, mock_delegation_config
    ):
        """
        Caller can poll again after waiting response and get result when ready.

        Given first poll returns waiting (job not ready)
        When callback arrives and caller polls again
        Then the second poll returns the completed result
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
            JobResult,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("retry-job-001")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            # First poll — job not ready, returns waiting immediately
            response1 = await handle_poll_delegation_job(
                {"job_id": "retry-job-001"},
                test_user,
            )

            data1 = json.loads(response1["content"][0]["text"])
            assert data1["status"] == "waiting"
            assert data1["continue_polling"] is True

            # Callback arrives (simulating Claude Server posting back)
            job_result = JobResult(
                job_id="retry-job-001",
                status="completed",
                output="The authentication module uses JWT tokens with RSA-256 signing.",
                exit_code=0,
                error=None,
            )
            await tracker.complete_job(job_result)

            # Second poll gets the result immediately
            response2 = await handle_poll_delegation_job(
                {"job_id": "retry-job-001"},
                test_user,
            )

            data2 = json.loads(response2["content"][0]["text"])
            assert data2["status"] == "completed"
            assert "JWT tokens" in data2["result"]
            assert data2["continue_polling"] is False

    @pytest.mark.asyncio
    async def test_poll_returns_error_for_job_not_in_tracker(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job returns error when job not found in tracker.

        Given a job_id that is not registered in the tracker
        When poll_delegation_job is called
        Then it returns an error indicating job not found
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_poll_delegation_job(
                {"job_id": "nonexistent-tracker-job"},
                test_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False
        assert (
            "not found" in data["error"].lower() or "expired" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_poll_can_retrieve_result_multiple_times(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job can retrieve the same result multiple times.

        Given a completed job
        When poll_delegation_job is called multiple times
        Then each call returns the result (not "not found" after first retrieval)
        This verifies the non-blocking get_result does NOT remove the job.
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
            JobResult,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("repeatable-job-002")

        job_result = JobResult(
            job_id="repeatable-job-002",
            status="completed",
            output="Result that can be retrieved multiple times.",
            exit_code=0,
            error=None,
        )
        await tracker.complete_job(job_result)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            # First retrieval
            response1 = await handle_poll_delegation_job(
                {"job_id": "repeatable-job-002"},
                test_user,
            )
            data1 = json.loads(response1["content"][0]["text"])
            assert data1["status"] == "completed"

            # Second retrieval — should still work (result cached, not removed)
            response2 = await handle_poll_delegation_job(
                {"job_id": "repeatable-job-002"},
                test_user,
            )
            data2 = json.loads(response2["content"][0]["text"])
            assert data2["status"] == "completed"
            assert "multiple times" in data2["result"]


class TestPollDelegationJobTimeoutBackwardCompat:
    """Tests verifying timeout_seconds is silently ignored (backward compat)."""

    @pytest.fixture(autouse=True)
    def reset_tracker_singleton(self):
        """Reset DelegationJobTracker singleton between tests."""
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
        )

        DelegationJobTracker._instance = None
        yield
        DelegationJobTracker._instance = None

    @pytest.mark.asyncio
    async def test_timeout_seconds_is_silently_ignored(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job silently ignores timeout_seconds parameter.

        Given timeout_seconds is provided (any value)
        When poll_delegation_job is called
        Then it returns normally without error about timeout_seconds
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
            JobResult,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("job-with-timeout-param")

        result = JobResult(
            job_id="job-with-timeout-param",
            status="completed",
            output="Test result",
            exit_code=0,
            error=None,
        )
        await tracker.complete_job(result)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            # Various timeout_seconds values — all silently ignored
            for timeout_val in [45, 0.005, 500, "fast", None]:
                args = {"job_id": "job-with-timeout-param"}
                if timeout_val is not None:
                    args["timeout_seconds"] = timeout_val  # type: ignore[assignment]

                response = await handle_poll_delegation_job(args, test_user)
                data = json.loads(response["content"][0]["text"])
                # Should succeed — timeout_seconds is ignored, not validated
                assert data.get("status") == "completed", (
                    f"Expected completed for timeout_seconds={timeout_val!r}, "
                    f"got: {data}"
                )

    @pytest.mark.asyncio
    async def test_poll_works_without_timeout_seconds(
        self, test_user, mock_delegation_config
    ):
        """
        poll_delegation_job works when timeout_seconds is not provided.

        Given timeout_seconds is not in args
        When poll_delegation_job is called
        Then it returns normally
        """
        from code_indexer.server.mcp.handlers import handle_poll_delegation_job
        from code_indexer.server.services.delegation_job_tracker import (
            DelegationJobTracker,
            JobResult,
        )

        tracker = DelegationJobTracker.get_instance()
        await tracker.register_job("job-no-timeout")

        result = JobResult(
            job_id="job-no-timeout",
            status="completed",
            output="Test",
            exit_code=0,
            error=None,
        )
        await tracker.complete_job(result)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "code_indexer.server.mcp.handlers._get_delegation_config",
                lambda: mock_delegation_config,
            )

            response = await handle_poll_delegation_job(
                {"job_id": "job-no-timeout"},  # No timeout_seconds
                test_user,
            )

        data = json.loads(response["content"][0]["text"])
        assert data["status"] == "completed"
