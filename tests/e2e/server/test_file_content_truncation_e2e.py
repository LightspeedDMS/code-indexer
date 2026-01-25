"""
End-to-end tests for file content truncation with cache handle (Story #33).

AC7: E2E test validates complete flow without mocking:
1. Create a real large file on disk
2. Request file via MCP get_file_content
3. Receive cache_handle in response
4. Retrieve full content via get_cached_content
5. Verify retrieved content matches original file

Following TDD methodology and anti-mock principle - real systems only.
Uses global repo infrastructure to avoid any mocking of repository lookup.
"""

import json
import os
import pytest
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.services.config_service import (
    get_config_service,
    reset_config_service,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP wrapper."""
    if "content" in mcp_response and len(mcp_response["content"]) > 0:
        content = mcp_response["content"][0]
        if "text" in content:
            try:
                return json.loads(content["text"])
            except json.JSONDecodeError:
                return {"text": content["text"]}
    return mcp_response


@pytest.fixture
def real_user():
    """Create a real user (not mocked) for testing."""
    return User(
        username="e2e_test_user",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash_for_testing",
        created_at=datetime.now(),
    )


@pytest.fixture
def e2e_truncation_environment():
    """Set up complete E2E environment with global repo infrastructure.

    Creates:
    - Real global repo directory structure with registry and aliases
    - Real test repository with files
    - Real PayloadCache with SQLite database
    - Properly configured content limits

    Directory structure matches server expectations:
    - server_data_dir/golden-repos (golden_repos_dir)
    - server_data_dir/cidx_server.db (SQLite database)
    """
    from code_indexer.server import app as app_module
    from code_indexer.server.services.file_service import FileListingService

    test_dir = tempfile.mkdtemp()

    # Directory structure matching server expectations:
    # golden_repos_dir is a subdirectory of server_data_dir
    server_data_dir = Path(test_dir) / "data"
    server_data_dir.mkdir(parents=True)

    golden_repos_dir = server_data_dir / "golden-repos"
    golden_repos_dir.mkdir(parents=True)
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True)

    repo_path = Path(test_dir) / "test_repo"
    repo_path.mkdir(parents=True)

    cache_dir = Path(test_dir) / "cache"
    cache_dir.mkdir(parents=True)

    # Save original state
    original_env = os.environ.get("CIDX_SERVER_DATA_DIR")
    original_golden_dir = getattr(app_module.app.state, "golden_repos_dir", None)
    original_payload_cache = getattr(app_module.app.state, "payload_cache", None)
    original_file_service = getattr(app_module, "file_service", None)

    # Configure environment - point to server_data_dir (parent of golden-repos)
    os.environ["CIDX_SERVER_DATA_DIR"] = str(server_data_dir)
    reset_config_service()

    # Initialize SQLite database with all required tables
    # Database goes in server_data_dir (same as get_server_global_registry expects)
    db_path = server_data_dir / "cidx_server.db"
    db_schema = DatabaseSchema(str(db_path))
    db_schema.initialize_database()

    # Set up global repo infrastructure - register test-global as a global repo
    # Use SQLite backend (as required by server handlers)
    registry = GlobalRegistry(
        str(golden_repos_dir),
        use_sqlite=True,
        db_path=str(db_path),
    )
    registry.register_global_repo(
        repo_name="test",
        alias_name="test-global",
        index_path=str(repo_path),
        repo_url=None,
    )

    # Create alias pointing to repo path
    alias_manager = AliasManager(str(aliases_dir))
    alias_manager.create_alias(
        alias_name="test-global",
        target_path=str(repo_path),
        repo_name="test",
    )

    # Inject golden_repos_dir into app state
    app_module.app.state.golden_repos_dir = str(golden_repos_dir)

    # Create real FileListingService
    app_module.file_service = FileListingService()

    yield {
        "test_dir": test_dir,
        "repo_path": repo_path,
        "golden_repos_dir": golden_repos_dir,
        "server_data_dir": server_data_dir,
        "cache_dir": cache_dir,
        "app_module": app_module,
        "original_payload_cache": original_payload_cache,
        "original_golden_dir": original_golden_dir,
        "original_file_service": original_file_service,
        "original_env": original_env,
    }

    # Cleanup - restore original state
    reset_config_service()
    if original_env is not None:
        os.environ["CIDX_SERVER_DATA_DIR"] = original_env
    else:
        os.environ.pop("CIDX_SERVER_DATA_DIR", None)

    if original_golden_dir is not None:
        app_module.app.state.golden_repos_dir = original_golden_dir
    if original_file_service is not None:
        app_module.file_service = original_file_service

    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)


@pytest.mark.e2e
class TestFileContentTruncationE2E:
    """E2E tests for file content truncation with cache handle (AC7)."""

    @pytest.mark.asyncio
    async def test_complete_truncation_workflow_with_cache_retrieval(
        self, real_user, e2e_truncation_environment
    ):
        """AC7: Complete workflow - create file, get truncated, retrieve via cache.

        Steps:
        1. Create a real large file on disk
        2. Request file via MCP get_file_content (using global repo path)
        3. Receive cache_handle in response
        4. Retrieve full content via get_cached_content
        5. Verify retrieved content matches original file
        """
        from code_indexer.server.mcp import handlers

        env = e2e_truncation_environment
        repo_path = env["repo_path"]
        cache_dir = env["cache_dir"]
        app_module = env["app_module"]

        # Step 1: Create a file that is under file service line limit (500 lines)
        # but large enough in characters to trigger token-based truncation.
        # We create 100 lines with 200 chars each = 20000+ chars total
        # With 100 max tokens and 4 chars/token = 400 chars max before truncation
        large_file = repo_path / "large_source.py"
        lines = [f"# Line {i:03d}: {'x' * 190}\n" for i in range(100)]
        original_content = "".join(lines)
        large_file.write_text(original_content)

        # Configure token limits:
        # - file_content_limits_config: High limit so file service doesn't truncate
        # - content_limits_config: Low limit (100 tokens * 4 chars = 400 chars)
        #   This is what TruncationHelper uses for token-based truncation
        config_service = get_config_service()
        config = config_service.get_config()
        # High limit for file service (don't truncate at read time)
        config.file_content_limits_config.max_tokens_per_request = 100000
        config.file_content_limits_config.chars_per_token = 4
        # Low limit for TruncationHelper (triggers truncation and caching)
        config.content_limits_config.file_content_max_tokens = 100
        config.content_limits_config.chars_per_token = 4
        config_service.config_manager.save_config(config)

        # Create real PayloadCache
        cache_db_path = Path(cache_dir) / "payload_cache.db"
        cache_config = PayloadCacheConfig(max_fetch_size_chars=100000)
        payload_cache = PayloadCache(cache_db_path, cache_config)
        await payload_cache.initialize()

        try:
            app_module.app.state.payload_cache = payload_cache

            # Step 2: Request file via MCP get_file_content (using global repo)
            params = {"repository_alias": "test-global", "file_path": "large_source.py"}
            mcp_response = await handlers.get_file_content(params, real_user)
            data = _extract_response_data(mcp_response)

            # Step 3: Verify cache_handle is present
            assert data.get("success") is True, f"Request failed: {data}"
            cache_handle = data.get("cache_handle")
            truncated = data.get("truncated")

            assert cache_handle is not None, "Expected cache_handle for large file"
            assert truncated is True, "Expected truncated=True for large file"

            # Step 4: Retrieve full content via get_cached_content
            full_content = await self._retrieve_all_pages(handlers, cache_handle, real_user)

            # Step 5: Verify retrieved content matches original file
            assert full_content == original_content, (
                f"Content mismatch: retrieved {len(full_content)} chars, "
                f"expected {len(original_content)} chars"
            )
        finally:
            await payload_cache.close()
            if env["original_payload_cache"] is not None:
                app_module.app.state.payload_cache = env["original_payload_cache"]

    async def _retrieve_all_pages(self, handlers, cache_handle: str, user) -> str:
        """Helper to retrieve all pages from cache."""
        full_content = ""
        page = 0

        while True:
            params = {"handle": cache_handle, "page": page}
            response = await handlers.handle_get_cached_content(params, user)
            data = _extract_response_data(response)

            assert data.get("success") is True, f"Cache retrieval failed: {data}"
            full_content += data.get("content", "")

            if not data.get("has_more", False):
                break
            page += 1

        return full_content
