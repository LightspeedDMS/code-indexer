"""
Unit tests for Story #224: C8 removal from app.py.

C8: Remove _reindex_cidx_meta_background() function and the background
    thread launch block from bootstrap_cidx_meta() in app.py.
    RefreshScheduler handles indexing now.

Tests:
- test_reindex_background_function_removed: function no longer exists in app
- test_bootstrap_cidx_meta_no_background_thread: no reindex thread started
"""

from unittest.mock import MagicMock, patch
from pathlib import Path


class TestReindexBackgroundRemovedFromApp:
    """C8: _reindex_cidx_meta_background and thread launch must be removed."""

    def test_reindex_background_function_removed(self):
        """
        _reindex_cidx_meta_background() must no longer exist in app module.

        C8: RefreshScheduler handles cidx-meta indexing via versioned platform.
        """
        import code_indexer.server.app as app_module

        assert not hasattr(app_module, "_reindex_cidx_meta_background"), (
            "_reindex_cidx_meta_background() must be removed from app.py "
            "(C8: RefreshScheduler owns cidx-meta indexing now)"
        )

    def test_bootstrap_cidx_meta_no_background_reindex_thread(self, tmp_path):
        """
        bootstrap_cidx_meta() must NOT start a 'cidx-meta-reindex' background thread.

        C8: The thread block that called _reindex_cidx_meta_background must be removed.
        After removal, bootstrap_cidx_meta() only registers cidx-meta but does NOT
        launch any background reindexing thread.
        """
        from code_indexer.server.app import bootstrap_cidx_meta

        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)

        mock_golden_repo_manager = MagicMock()
        mock_golden_repo_manager.golden_repo_exists.return_value = True  # Already registered

        threads_started = []

        original_thread_init = None

        import threading

        class ThreadSpy(threading.Thread):
            def __init__(self, *args, **kwargs):
                threads_started.append(kwargs.get("name", "unnamed"))
                super().__init__(*args, **kwargs)

        with patch("threading.Thread", ThreadSpy):
            bootstrap_cidx_meta(mock_golden_repo_manager, golden_repos_dir)

        reindex_threads = [
            name for name in threads_started if "reindex" in name.lower()
        ]
        assert reindex_threads == [], (
            f"bootstrap_cidx_meta() must NOT start any reindex background thread. "
            f"Got threads: {reindex_threads}. "
            "(C8: background reindex thread block must be removed)"
        )
