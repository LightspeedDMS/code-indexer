"""
Unit tests for Story #224: C6 removal from meta_description_hook.py.

C6: Remove reindex_cidx_meta() function and _cidx_meta_index_lock from
    meta_description_hook.py. Writers must NOT trigger cidx index anymore;
    RefreshScheduler will handle versioned indexing instead.

Tests:
- test_reindex_cidx_meta_function_removed: reindex_cidx_meta not in module
- test_cidx_meta_index_lock_removed: _cidx_meta_index_lock not in module
- test_on_repo_added_does_not_call_cidx: on_repo_added() skips cidx
- test_on_repo_removed_does_not_call_cidx: on_repo_removed() skips cidx
- test_create_readme_fallback_does_not_call_cidx: fallback skips cidx
"""

from unittest.mock import MagicMock, patch


class TestReindexRemovedFromMetaDescriptionHook:
    """C6: reindex_cidx_meta and _cidx_meta_index_lock must be removed."""

    def test_reindex_cidx_meta_function_removed(self):
        """
        reindex_cidx_meta() must no longer exist in meta_description_hook module.

        C6: RefreshScheduler handles indexing now. Writers must not call cidx.
        """
        import code_indexer.global_repos.meta_description_hook as module

        assert not hasattr(module, "reindex_cidx_meta"), (
            "reindex_cidx_meta() must be removed from meta_description_hook "
            "(C6: RefreshScheduler handles indexing via versioned platform)"
        )

    def test_cidx_meta_index_lock_removed(self):
        """
        _cidx_meta_index_lock must no longer exist in meta_description_hook module.

        C6: The lock protected concurrent reindex calls; without reindex, the lock
        is unnecessary.
        """
        import code_indexer.global_repos.meta_description_hook as module

        assert not hasattr(module, "_cidx_meta_index_lock"), (
            "_cidx_meta_index_lock must be removed from meta_description_hook "
            "(C6: no concurrent reindex operations needed)"
        )

    def test_on_repo_added_does_not_call_cidx(self, tmp_path):
        """
        on_repo_added() must NOT invoke cidx index after C6 removal.

        Previously on_repo_added() called reindex_cidx_meta() which ran
        'cidx index'. After C6 that call is removed.
        """
        cidx_meta_path = tmp_path / "cidx-meta"
        cidx_meta_path.mkdir()

        repo_name = "test-repo"
        repo_url = "https://github.com/test/repo"
        clone_path = tmp_path / repo_name
        clone_path.mkdir()
        (clone_path / "README.md").write_text("# Test Repo")

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = True

        cidx_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "cidx" in cmd:
                cidx_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            with patch(
                "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
                return_value=mock_cli_manager,
            ):
                from code_indexer.global_repos.meta_description_hook import on_repo_added
                on_repo_added(
                    repo_name=repo_name,
                    repo_url=repo_url,
                    clone_path=str(clone_path),
                    golden_repos_dir=str(tmp_path),
                )

        assert cidx_calls == [], (
            "on_repo_added() must NOT call cidx after C6 removal. "
            f"Got cidx calls: {cidx_calls}"
        )

    def test_on_repo_removed_does_not_call_cidx(self, tmp_path):
        """
        on_repo_removed() must NOT invoke cidx index after C6 removal.

        Previously on_repo_removed() called reindex_cidx_meta() after deleting
        the .md file. After C6 that call is removed.
        """
        cidx_meta_path = tmp_path / "cidx-meta"
        cidx_meta_path.mkdir()

        repo_name = "test-repo"
        md_file = cidx_meta_path / f"{repo_name}.md"
        md_file.write_text("# Test Repo description")

        cidx_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "cidx" in cmd:
                cidx_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            from code_indexer.global_repos.meta_description_hook import on_repo_removed
            on_repo_removed(
                repo_name=repo_name,
                golden_repos_dir=str(tmp_path),
            )

        assert cidx_calls == [], (
            "on_repo_removed() must NOT call cidx after C6 removal. "
            f"Got cidx calls: {cidx_calls}"
        )

    def test_create_readme_fallback_does_not_call_cidx(self, tmp_path):
        """
        _create_readme_fallback() must NOT invoke cidx index after C6 removal.

        Previously _create_readme_fallback() called reindex_cidx_meta() after
        creating the README copy. After C6 that call is removed.
        """
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        (repo_path / "README.md").write_text("# Test README content")

        cidx_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            if isinstance(cmd, list) and "cidx" in cmd:
                cidx_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            from code_indexer.global_repos.meta_description_hook import _create_readme_fallback
            _create_readme_fallback(
                repo_path=repo_path,
                alias="test-repo",
                meta_dir=meta_dir,
            )

        assert cidx_calls == [], (
            "_create_readme_fallback() must NOT call cidx after C6 removal. "
            f"Got cidx calls: {cidx_calls}"
        )
