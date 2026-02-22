"""
Unit tests for ToolDocLoader - MCP Tool Documentation Loader.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC5: Loader Runtime Behavior - Caching and error handling.
"""

import pytest


@pytest.fixture
def temp_docs_dir(tmp_path):
    """Create a temporary tool_docs directory with category subdirectories."""
    docs_dir = tmp_path / "tool_docs"
    docs_dir.mkdir()
    for category in [
        "search",
        "git",
        "scip",
        "files",
        "admin",
        "repos",
        "ssh",
        "guides",
        "cicd",
    ]:
        (docs_dir / category).mkdir()
    return docs_dir


class TestToolDocLoaderBasicLoading:
    """Basic loading and caching behavior."""

    def test_load_all_docs_caches_results(self, temp_docs_dir):
        """load_all_docs() should cache parsed content."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "test_tool.md").write_text(
            "---\nname: test_tool\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Test tool.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        docs = loader.load_all_docs()
        assert loader._loaded is True
        assert "test_tool" in docs

        docs2 = loader.load_all_docs()
        assert docs2 is docs  # Same object returned (cached)

    def test_get_description_returns_cached_content(self, temp_docs_dir):
        """get_description() should return cached markdown body."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "my_tool.md").write_text(
            "---\nname: my_tool\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: My tool.\n---\n\nThis is the description body.\nMultiple lines."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        description = loader.get_description("my_tool")

        assert "This is the description body." in description
        assert "Multiple lines." in description

    def test_get_description_raises_for_missing_tool(self, temp_docs_dir):
        """get_description() should raise ToolDocNotFoundError for missing tool."""
        from code_indexer.server.mcp.tool_doc_loader import (
            ToolDocLoader,
            ToolDocNotFoundError,
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader._loaded = True

        with pytest.raises(ToolDocNotFoundError) as exc_info:
            loader.get_description("nonexistent_tool")
        assert "nonexistent_tool" in str(exc_info.value)

    def test_empty_docs_directory_results_in_empty_cache(self, temp_docs_dir):
        """Empty docs directory should result in empty cache."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(temp_docs_dir)
        docs = loader.load_all_docs()

        assert docs == {}
        assert loader._loaded is True


class TestToolDocLoaderPermissionAndParams:
    """Permission and parameter retrieval."""

    def test_get_permission_returns_required_permission(self, temp_docs_dir):
        """get_permission() returns required_permission from frontmatter."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        admin_dir = temp_docs_dir / "admin"
        (admin_dir / "admin_tool.md").write_text(
            "---\nname: admin_tool\ncategory: admin\n"
            "required_permission: manage_golden_repos\ntl_dr: Admin tool.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader.get_permission("admin_tool") == "manage_golden_repos"

    def test_get_param_description_returns_parameter_doc(self, temp_docs_dir):
        """get_param_description() returns specific parameter documentation."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "param_tool.md").write_text(
            "---\nname: param_tool\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Tool with params.\nparameters:\n  query_text: The search query.\n"
            "  limit: Max results.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        assert (
            loader.get_param_description("param_tool", "query_text")
            == "The search query."
        )
        assert loader.get_param_description("param_tool", "limit") == "Max results."

    def test_get_param_description_returns_none_for_missing(self, temp_docs_dir):
        """get_param_description() returns None for nonexistent parameter."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "no_params.md").write_text(
            "---\nname: no_params\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: No params.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader.get_param_description("no_params", "nonexistent") is None

    def test_validate_against_registry_finds_missing(self, temp_docs_dir):
        """validate_against_registry() checks all TOOL_REGISTRY tools have docs."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_code.md").write_text(
            "---\nname: search_code\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Search code.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        missing = loader.validate_against_registry(TOOL_REGISTRY)
        assert len(missing) == len(TOOL_REGISTRY) - 1
        assert "search_code" not in missing


class TestSingletonThreadSafety:
    """Thread-safety of the _get_tool_doc_loader() singleton."""

    def test_singleton_lock_variable_exists(self):
        """_singleton_lock must exist at module level and be a lock object."""
        import threading
        import code_indexer.server.mcp.tool_doc_loader as module

        assert hasattr(module, "_singleton_lock"), (
            "_singleton_lock module variable is missing"
        )
        # threading.Lock() returns a _thread.lock (or _thread.RLock) instance;
        # the public API guarantees it has acquire/release methods.
        lock = module._singleton_lock
        assert hasattr(lock, "acquire") and hasattr(lock, "release"), (
            "_singleton_lock does not look like a threading.Lock"
        )
        # Verify it is one of the standard lock types
        assert isinstance(lock, type(threading.Lock())), (
            f"_singleton_lock is {type(lock)}, expected threading.Lock type"
        )

    def test_get_tool_doc_loader_is_thread_safe(self, tmp_path):
        """Concurrent calls to _get_tool_doc_loader() must create only one loader.

        Strategy: patch the module's _singleton_loader back to None, then replace
        ToolDocLoader with a counting subclass, fire 20 threads, verify exactly
        one load_all_docs() call happened.
        """
        import threading
        import code_indexer.server.mcp.tool_doc_loader as module
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Build a minimal but valid tool_docs directory so load_all_docs() succeeds
        docs_dir = tmp_path / "tool_docs"
        docs_dir.mkdir()
        search_dir = docs_dir / "search"
        search_dir.mkdir()
        (search_dir / "dummy_tool.md").write_text(
            "---\nname: dummy_tool\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Dummy.\n---\n\nDescription."
        )

        load_call_count = [0]
        count_lock = threading.Lock()

        class CountingLoader(ToolDocLoader):
            def load_all_docs(self):
                with count_lock:
                    load_call_count[0] += 1
                return super().load_all_docs()

        original_loader_class = module.ToolDocLoader
        original_singleton = module._singleton_loader
        original_docs_dir_factory = None  # we patch via ToolDocLoader class

        try:
            # Reset singleton so _get_tool_doc_loader() will create a new one
            module._singleton_loader = None
            # Replace the class so the factory instantiates our counter
            module.ToolDocLoader = CountingLoader  # type: ignore[attr-defined]
            # Also patch the docs_dir path inside _get_tool_doc_loader by
            # overriding __file__-based resolution: replace docs_dir inline
            # via a wrapper that injects our tmp docs_dir.
            original_get = module._get_tool_doc_loader

            def patched_get():
                global _singleton_loader  # noqa: F841
                if module._singleton_loader is None:
                    with module._singleton_lock:
                        if module._singleton_loader is None:
                            loader = CountingLoader(docs_dir)
                            loader.load_all_docs()
                            module._singleton_loader = loader
                return module._singleton_loader

            module._get_tool_doc_loader = patched_get  # type: ignore[attr-defined]

            results = []
            errors = []

            def call_getter():
                try:
                    results.append(module._get_tool_doc_loader())
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=call_getter) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Threads raised exceptions: {errors}"
            assert len(results) == 20, "Not all threads got a result"
            # All threads must have received the SAME singleton instance
            first = results[0]
            assert all(r is first for r in results), (
                "Different loader instances returned - singleton is not thread-safe"
            )
            # load_all_docs() must have been called exactly once
            assert load_call_count[0] == 1, (
                f"load_all_docs() was called {load_call_count[0]} times, expected 1"
            )
        finally:
            # Restore module state unconditionally
            module._singleton_loader = original_singleton
            module.ToolDocLoader = original_loader_class  # type: ignore[attr-defined]
            module._get_tool_doc_loader = original_get  # type: ignore[attr-defined]
