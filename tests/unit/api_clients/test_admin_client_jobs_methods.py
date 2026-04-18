"""Unit tests for AdminAPIClient jobs-related methods."""

import inspect
import pytest

from src.code_indexer.api_clients.admin_client import AdminAPIClient


# ---------------------------------------------------------------------------
# Bug #749 sibling: AdminAPIClient methods called via run_async() in CLI must
# be async coroutines, otherwise: ValueError: a coroutine was expected, got {}
# ---------------------------------------------------------------------------
class TestAdminAPIClientAsyncContracts:
    """Bug #749 sibling: AdminAPIClient run_async()-called methods must be async."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "get_user",
            "update_user",
            "delete_user",
            "change_user_password",
            "list_golden_repositories",
            "refresh_golden_repository",
            "get_golden_repository_branches",
            "add_index_to_golden_repo",
            "get_golden_repo_indexes",
        ],
    )
    def test_method_is_coroutine(self, method_name: str):
        """Each method called via run_async() must be async def."""
        method = getattr(AdminAPIClient, method_name)
        assert inspect.iscoroutinefunction(method), (
            f"AdminAPIClient.{method_name} must be async def "
            f"so run_async(admin_client.{method_name}(...)) works (Bug #749 sibling)"
        )


class TestAdminAPIClientJobsMethods:
    """Tests for AdminAPIClient jobs cleanup methods."""

    @pytest.mark.asyncio
    async def test_cleanup_jobs_method_exists(self):
        """Test that AdminAPIClient has cleanup_jobs method."""
        client = AdminAPIClient(
            server_url="http://test",
            credentials={"username": "admin", "password": "pass"},
            project_root="/test",  # type: ignore[arg-type]
        )

        # Check method exists
        assert hasattr(client, "cleanup_jobs"), (
            "AdminAPIClient should have cleanup_jobs method"
        )
        assert callable(getattr(client, "cleanup_jobs", None)), (
            "cleanup_jobs should be callable"
        )
