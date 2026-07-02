"""Tests for _get_repo_root() memoization in FilesystemVectorStore.

Story #677: per-call git subprocess for invariant data causes test timeouts
under load. The git repo root does not change during the lifetime of a
FilesystemVectorStore instance, so it must be memoized.
"""

from unittest.mock import patch, MagicMock

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


class TestGetRepoRootMemoization:
    """Verify that _get_repo_root() is memoized across calls."""

    @staticmethod
    def _count_git_rev_parse_show_toplevel_calls(mock_run) -> int:
        """Count calls to subprocess.run with git rev-parse --show-toplevel args."""
        return sum(
            1
            for call in mock_run.call_args_list
            if call.args
            and len(call.args[0]) >= 3
            and call.args[0][:3] == ["git", "rev-parse", "--show-toplevel"]
        )

    def test_get_repo_root_called_once_across_many_direct_calls(self, tmp_path):
        """subprocess.run must be called at most once for git rev-parse --show-toplevel
        even when _get_repo_root() is called 50 times directly.

        This locks in the memoization contract: the first subprocess result must
        be cached and reused for all subsequent invocations.
        """
        store = FilesystemVectorStore(tmp_path / "index", project_root=tmp_path)

        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = str(tmp_path) + "\n"

        with patch(
            "code_indexer.storage.filesystem_vector_store.subprocess.run",
            return_value=git_result,
        ) as mock_run:
            for _ in range(50):
                store._get_repo_root()

            git_calls = self._count_git_rev_parse_show_toplevel_calls(mock_run)
            assert git_calls <= 1, (
                f"Expected subprocess.run to be called at most once for git rev-parse "
                f"--show-toplevel, but it was called {git_calls} times. "
                f"_get_repo_root() is not memoized."
            )

    def test_get_repo_root_negative_cached(self, tmp_path):
        """When project_root is not a git repo (returncode=1), the None result
        must be cached. subprocess.run must be called exactly once even when
        _get_repo_root() is called 10 times.
        """
        store = FilesystemVectorStore(tmp_path / "index", project_root=tmp_path)

        not_git_result = MagicMock()
        not_git_result.returncode = 1
        not_git_result.stdout = ""
        not_git_result.stderr = "fatal: not a git repository"

        with patch(
            "code_indexer.storage.filesystem_vector_store.subprocess.run",
            return_value=not_git_result,
        ) as mock_run:
            results = [store._get_repo_root() for _ in range(10)]

            assert all(r is None for r in results), (
                "Expected all calls to return None for non-git directory"
            )

            git_calls = self._count_git_rev_parse_show_toplevel_calls(mock_run)
            assert git_calls == 1, (
                f"Expected subprocess.run to be called exactly once (None result cached), "
                f"but it was called {git_calls} times. "
                f"_get_repo_root() does not cache negative results."
            )
