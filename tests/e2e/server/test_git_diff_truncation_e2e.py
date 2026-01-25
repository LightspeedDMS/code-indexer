"""
End-to-end tests for git diff truncation with cache handle (Story #34).

Tests complete workflow without mocking:
1. Create a real git repository with large diff between commits
2. Request diff via MCP git_diff handler
3. Receive cache_handle in response when diff exceeds token limit
4. Retrieve full diff content via get_cached_content
5. Verify retrieved content matches original diff data

Following TDD methodology and anti-mock principle - real systems only.
Uses global repo infrastructure to avoid any mocking of repository lookup.
"""

import json
import os
import pytest
import shutil
import subprocess
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
        username="e2e_git_diff_test_user",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash_for_testing",
        created_at=datetime.now(),
    )


@pytest.fixture
def e2e_git_diff_environment():
    """Set up complete E2E environment with git repository for diff testing.

    Creates:
    - Real global repo directory structure with registry and aliases
    - Real git repository with two commits (large diff between them)
    - Real PayloadCache with SQLite database
    - Properly configured content limits

    Directory structure matches server expectations:
    - server_data_dir/golden-repos (golden_repos_dir)
    - server_data_dir/cidx_server.db (SQLite database)
    """
    from code_indexer.server import app as app_module

    test_dir = tempfile.mkdtemp()

    # Directory structure matching server expectations
    server_data_dir = Path(test_dir) / "data"
    server_data_dir.mkdir(parents=True)

    golden_repos_dir = server_data_dir / "golden-repos"
    golden_repos_dir.mkdir(parents=True)
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True)

    repo_path = Path(test_dir) / "test_git_repo"
    repo_path.mkdir(parents=True)

    cache_dir = Path(test_dir) / "cache"
    cache_dir.mkdir(parents=True)

    # Initialize git repository
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # First commit - create initial files
    (repo_path / "initial.py").write_text("# Initial file\nprint('hello')\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    first_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    first_commit = first_result.stdout.strip()

    # Second commit - create many files with lots of content (large diff)
    # Create 20 files with 50 lines each to ensure large diff
    for i in range(20):
        file_path = repo_path / f"module_{i:02d}.py"
        lines = [f"# Module {i} - Line {j}\n" for j in range(50)]
        lines.append(f"def function_{i}():\n")
        lines.append(f"    '''Module {i} function.'''\n")
        lines.append(f"    return 'module_{i}'\n")
        file_path.write_text("".join(lines))

    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add many modules"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    second_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    second_commit = second_result.stdout.strip()

    # Save original state
    original_env = os.environ.get("CIDX_SERVER_DATA_DIR")
    original_golden_dir = getattr(app_module.app.state, "golden_repos_dir", None)
    original_payload_cache = getattr(app_module.app.state, "payload_cache", None)

    # Configure environment
    os.environ["CIDX_SERVER_DATA_DIR"] = str(server_data_dir)
    reset_config_service()

    # Initialize SQLite database with all required tables
    db_path = server_data_dir / "cidx_server.db"
    db_schema = DatabaseSchema(str(db_path))
    db_schema.initialize_database()

    # Set up global repo infrastructure
    registry = GlobalRegistry(
        str(golden_repos_dir),
        use_sqlite=True,
        db_path=str(db_path),
    )
    registry.register_global_repo(
        repo_name="test-git-diff",
        alias_name="test-git-diff-global",
        index_path=str(repo_path),
        repo_url=None,
    )

    # Create alias pointing to repo path
    alias_manager = AliasManager(str(aliases_dir))
    alias_manager.create_alias(
        alias_name="test-git-diff-global",
        target_path=str(repo_path),
        repo_name="test-git-diff",
    )

    # Inject golden_repos_dir into app state
    app_module.app.state.golden_repos_dir = str(golden_repos_dir)

    yield {
        "test_dir": test_dir,
        "repo_path": repo_path,
        "golden_repos_dir": golden_repos_dir,
        "server_data_dir": server_data_dir,
        "cache_dir": cache_dir,
        "app_module": app_module,
        "first_commit": first_commit,
        "second_commit": second_commit,
        "original_payload_cache": original_payload_cache,
        "original_golden_dir": original_golden_dir,
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

    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)


@pytest.mark.e2e
class TestGitDiffTruncationE2E:
    """E2E tests for git diff truncation with cache handle (Story #34)."""

    @pytest.mark.asyncio
    async def test_large_diff_returns_cache_handle_and_allows_retrieval(
        self, real_user, e2e_git_diff_environment
    ):
        """Complete workflow - create large diff, get truncated, retrieve via cache.

        Steps:
        1. Use git repo with large diff between commits
        2. Request diff via MCP git_diff handler
        3. Verify cache_handle is present in response
        4. Retrieve full content via get_cached_content
        5. Verify retrieved content is valid JSON with diff data
        """
        from code_indexer.server.mcp import handlers

        env = e2e_git_diff_environment
        cache_dir = env["cache_dir"]
        app_module = env["app_module"]
        first_commit = env["first_commit"]
        second_commit = env["second_commit"]

        # Configure low token limit to trigger truncation
        # 100 tokens * 4 chars/token = 400 chars max before truncation
        # Our diff should be much larger (20 files * ~50 lines each)
        config_service = get_config_service()
        config = config_service.get_config()
        config.content_limits_config.git_diff_max_tokens = 100
        config.content_limits_config.chars_per_token = 4
        config_service.config_manager.save_config(config)

        # Create real PayloadCache
        cache_db_path = Path(cache_dir) / "payload_cache.db"
        cache_config = PayloadCacheConfig(max_fetch_size_chars=100000)
        payload_cache = PayloadCache(cache_db_path, cache_config)
        await payload_cache.initialize()

        try:
            app_module.app.state.payload_cache = payload_cache

            # Request diff via MCP git_diff handler
            params = {
                "repository_alias": "test-git-diff-global",
                "from_revision": first_commit,
                "to_revision": second_commit,
            }
            mcp_response = await handlers.handle_git_diff(params, real_user)
            data = _extract_response_data(mcp_response)

            # Verify response success
            assert data.get("success") is True, f"Request failed: {data}"

            # Verify truncation metadata
            cache_handle = data.get("cache_handle")
            truncated = data.get("truncated")
            total_tokens = data.get("total_tokens")
            has_more = data.get("has_more")

            assert cache_handle is not None, "Expected cache_handle for large diff"
            assert truncated is True, "Expected truncated=True for large diff"
            assert total_tokens > 100, f"Expected total_tokens > 100, got {total_tokens}"
            assert has_more is True, "Expected has_more=True for truncated diff"

            # Verify backward-compatible fields still present
            assert "files" in data
            assert "total_insertions" in data
            assert "total_deletions" in data
            assert data["total_insertions"] > 0

            # Retrieve full content via get_cached_content
            full_content = await self._retrieve_all_pages(
                handlers, cache_handle, real_user
            )

            # Verify retrieved content is valid JSON with diff structure
            full_data = json.loads(full_content)
            assert "files" in full_data
            assert "from_revision" in full_data
            assert "to_revision" in full_data
            assert len(full_data["files"]) == 20  # We created 20 new files

        finally:
            await payload_cache.close()
            if env["original_payload_cache"] is not None:
                app_module.app.state.payload_cache = env["original_payload_cache"]

    @pytest.mark.asyncio
    async def test_small_diff_no_truncation_no_cache_handle(
        self, real_user, e2e_git_diff_environment
    ):
        """Small diff should not trigger truncation or caching.

        With high token limit, small diffs should return:
        - cache_handle = None
        - truncated = False
        - All files in response
        """
        from code_indexer.server.mcp import handlers

        env = e2e_git_diff_environment
        cache_dir = env["cache_dir"]
        app_module = env["app_module"]
        first_commit = env["first_commit"]
        second_commit = env["second_commit"]

        # Configure HIGH token limit - no truncation expected
        config_service = get_config_service()
        config = config_service.get_config()
        config.content_limits_config.git_diff_max_tokens = 100000
        config.content_limits_config.chars_per_token = 4
        config_service.config_manager.save_config(config)

        # Create real PayloadCache
        cache_db_path = Path(cache_dir) / "payload_cache_small.db"
        cache_config = PayloadCacheConfig(max_fetch_size_chars=100000)
        payload_cache = PayloadCache(cache_db_path, cache_config)
        await payload_cache.initialize()

        try:
            app_module.app.state.payload_cache = payload_cache

            # Request diff via MCP git_diff handler
            params = {
                "repository_alias": "test-git-diff-global",
                "from_revision": first_commit,
                "to_revision": second_commit,
            }
            mcp_response = await handlers.handle_git_diff(params, real_user)
            data = _extract_response_data(mcp_response)

            # Verify response success
            assert data.get("success") is True, f"Request failed: {data}"

            # Verify NO truncation
            cache_handle = data.get("cache_handle")
            truncated = data.get("truncated")

            assert cache_handle is None, "Expected no cache_handle for small diff"
            assert truncated is False, "Expected truncated=False for small diff"

            # All files should be present
            assert len(data["files"]) == 20
            assert data["total_insertions"] > 0

        finally:
            await payload_cache.close()
            if env["original_payload_cache"] is not None:
                app_module.app.state.payload_cache = env["original_payload_cache"]

    async def _retrieve_all_pages(self, handlers, cache_handle: str, user) -> str:
        """Helper to retrieve all pages from cache.

        Note: get_cached_content uses 0-indexed pages and 'handle' parameter name.
        """
        full_content = ""
        page = 0
        while True:
            # Use correct parameter name 'handle' (not 'cache_handle')
            params = {"handle": cache_handle, "page": page}
            response = await handlers.handle_get_cached_content(params, user)
            data = _extract_response_data(response)

            if not data.get("success", False):
                break

            content = data.get("content", "")
            if not content:
                break

            full_content += content

            if not data.get("has_more", False):
                break

            page += 1

        return full_content
