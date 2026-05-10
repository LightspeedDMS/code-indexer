"""
Concurrency tests for Story #987 AC5:
  - Both singleton and _extended_cache populated atomically under threading.Lock
  - 10 concurrent threads hitting _get_tool_doc_loader() see the same instance
  - get_extended_description() is safe under concurrent access

Uses real ToolDocLoader (no mocking).
"""

import queue
import threading
from pathlib import Path


def _make_minimal_docs_dir(tmp_path: Path) -> Path:
    """Create a minimal tool_docs directory with one tool for testing."""
    docs_dir = tmp_path / "tool_docs"
    search_dir = docs_dir / "search"
    search_dir.mkdir(parents=True)
    (search_dir / "concurrent_tool.md").write_text(
        "---\n"
        "name: concurrent_tool\n"
        "category: search\n"
        "required_permission: query_repos\n"
        "tl_dr: Concurrent test tool.\n"
        "---\n\n"
        "Full body of the concurrent test tool.\n",
        encoding="utf-8",
    )
    return docs_dir


class TestToolDocLoaderConcurrency:
    """AC5: Singleton and extended cache are safe under concurrent access."""

    def test_ten_concurrent_threads_get_same_singleton(self):
        """10 concurrent threads calling _get_tool_doc_loader() get the same object."""
        import code_indexer.server.mcp.tool_doc_loader as loader_module
        from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader

        result_queue: queue.Queue = queue.Queue()
        error_queue: queue.Queue = queue.Queue()
        barrier = threading.Barrier(10)

        def load_singleton():
            try:
                barrier.wait()  # synchronize all threads to start simultaneously
                loader = _get_tool_doc_loader()
                result_queue.put(loader)
            except Exception as e:
                error_queue.put(e)

        threads = [threading.Thread(target=load_singleton) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert error_queue.empty(), (
            f"Errors in concurrent threads: {list(error_queue.queue)}"
        )
        loaders = list(result_queue.queue)
        assert len(loaders) == 10, f"Expected 10 results, got {len(loaders)}"

        # All threads must get the exact same singleton instance
        first = loaders[0]
        assert all(loader is first for loader in loaders), (
            "Not all threads received the same singleton instance"
        )
        # Singleton must be the module-level one
        assert first is loader_module._singleton_loader

    def test_ten_concurrent_threads_get_extended_description_safely(self, tmp_path):
        """10 concurrent threads calling get_extended_description() return correct results."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        docs_dir = _make_minimal_docs_dir(tmp_path)
        loader = ToolDocLoader(docs_dir)
        loader.load_all_docs()

        result_queue: queue.Queue = queue.Queue()
        error_queue: queue.Queue = queue.Queue()
        barrier = threading.Barrier(10)

        def fetch_extended():
            try:
                barrier.wait()  # synchronize all threads to start simultaneously
                result = loader.get_extended_description("concurrent_tool")
                result_queue.put(result)
            except Exception as e:
                error_queue.put(e)

        threads = [threading.Thread(target=fetch_extended) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert error_queue.empty(), (
            f"Errors in concurrent threads: {list(error_queue.queue)}"
        )
        results = list(result_queue.queue)
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

        # All results must be the full body
        for result in results:
            assert result == "Full body of the concurrent test tool.\n"

    def test_module_level_extended_cache_is_populated_after_get_extended_description(
        self, tmp_path
    ):
        """After get_extended_description() call, module-level _extended_cache is populated."""
        import code_indexer.server.mcp.tool_doc_loader as loader_module

        docs_dir = _make_minimal_docs_dir(tmp_path)
        loader = loader_module.ToolDocLoader(docs_dir)
        loader.load_all_docs()

        # Pre-condition: remove key from cache so we test the population path
        loader_module._extended_cache.pop("concurrent_tool", None)

        result = loader.get_extended_description("concurrent_tool")
        assert result == "Full body of the concurrent test tool.\n"

        # Post-condition: _extended_cache now contains the tool
        assert "concurrent_tool" in loader_module._extended_cache
        assert (
            loader_module._extended_cache["concurrent_tool"]
            == "Full body of the concurrent test tool.\n"
        )

    def test_loader_lock_exists_at_module_level(self):
        """_loader_lock must be a threading.Lock at module level (AC5)."""
        import code_indexer.server.mcp.tool_doc_loader as loader_module

        assert hasattr(loader_module, "_loader_lock"), (
            "_loader_lock must exist at module level"
        )
        # threading.Lock() returns a _thread.lock type; check it has acquire/release
        lock = loader_module._loader_lock
        assert hasattr(lock, "acquire"), "_loader_lock must have acquire()"
        assert hasattr(lock, "release"), "_loader_lock must have release()"

    def test_extended_cache_exists_at_module_level(self):
        """_extended_cache must be a dict at module level (AC5)."""
        import code_indexer.server.mcp.tool_doc_loader as loader_module

        assert hasattr(loader_module, "_extended_cache"), (
            "_extended_cache must exist at module level"
        )
        assert isinstance(loader_module._extended_cache, dict), (
            "_extended_cache must be a dict"
        )
