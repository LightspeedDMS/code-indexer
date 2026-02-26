"""Tests for wiki invalidation hook wiring (Story #304).

Validates that mutation handlers trigger wiki_cache_invalidator
after successful operations (AC1, AC2, AC8, AC9).
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator


@pytest.fixture
def invalidator_with_cache():
    """Return a fresh WikiCacheInvalidator wired to a real WikiCache."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cache = WikiCache(path)
    cache.ensure_tables()
    invalidator = WikiCacheInvalidator()
    invalidator.set_wiki_cache(cache)
    yield invalidator, cache, path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestFileCRUDInvalidationHookAC1:
    """AC1: File CRUD triggers wiki cache invalidation (only .md/.markdown/.txt)."""

    def test_create_md_file_triggers_invalidation(self, invalidator_with_cache):
        """Creating a .md file must trigger wiki cache invalidation via the invalidator."""
        invalidator, cache, _ = invalidator_with_cache
        # Pre-populate sidebar cache
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page.md").write_text("# Page")
            cache.put_sidebar("my-repo", [{"title": "Page"}], repo_dir)

            # Verify it's cached
            assert cache.get_sidebar("my-repo", repo_dir) is not None

            # Trigger invalidation as the create_file hook would
            invalidator.invalidate_for_file_change("my-repo", "new-page.md")

            # Sidebar cache must now be cleared
            assert cache.get_sidebar("my-repo", repo_dir) is None

    def test_create_py_file_does_not_trigger_invalidation(self, invalidator_with_cache):
        """Creating a .py file must NOT trigger wiki cache invalidation (AC5)."""
        invalidator, cache, _ = invalidator_with_cache
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page.md").write_text("# Page")
            cache.put_sidebar("my-repo", [{"title": "Page"}], repo_dir)

            invalidator.invalidate_for_file_change("my-repo", "src/module.py")

            # Sidebar cache must NOT be cleared
            assert cache.get_sidebar("my-repo", repo_dir) is not None

    def test_edit_md_file_triggers_invalidation(self, invalidator_with_cache):
        """Editing a .md file must trigger wiki cache invalidation."""
        invalidator, cache, _ = invalidator_with_cache
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page.md").write_text("# Page")
            cache.put_sidebar("my-repo", [{"title": "Page"}], repo_dir)

            invalidator.invalidate_for_file_change("my-repo", "page.md")

            assert cache.get_sidebar("my-repo", repo_dir) is None

    def test_delete_txt_file_triggers_invalidation(self, invalidator_with_cache):
        """Deleting a .txt file must trigger wiki cache invalidation."""
        invalidator, cache, _ = invalidator_with_cache
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page.md").write_text("# Page")
            cache.put_sidebar("my-repo", [{"title": "Page"}], repo_dir)

            invalidator.invalidate_for_file_change("my-repo", "notes.txt")

            assert cache.get_sidebar("my-repo", repo_dir) is None


class TestHandlersCallInvalidator:
    """Tests that handlers.py calls wiki_cache_invalidator after successful operations."""

    def test_handle_create_file_calls_invalidator_for_md(self):
        """handle_create_file must call wiki_cache_invalidator.invalidate_for_file_change."""
        from code_indexer.server.mcp.handlers import handle_create_file
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as mock_crud:
            mock_crud.is_write_exception.return_value = False
            mock_crud.create_file.return_value = {"success": True}
            with patch(
                "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
            ) as mock_arm_cls:
                mock_arm = MagicMock()
                mock_arm.get_activated_repo_path.return_value = "/fake/repo"
                mock_arm_cls.return_value = mock_arm
                with patch(
                    "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers.app_module"
                    ) as mock_app_module:
                        mock_app_module.app.state.golden_repos_dir = "/fake/golden"
                        with patch(
                            "code_indexer.server.mcp.handlers._is_write_mode_active",
                            return_value=False,
                        ):
                            handle_create_file(
                                {
                                    "repository_alias": "my-repo",
                                    "file_path": "docs/guide.md",
                                    "content": "# Guide",
                                },
                                mock_user,
                            )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_handle_edit_file_calls_invalidator_for_md(self):
        """handle_edit_file must call wiki_cache_invalidator.invalidate_for_file_change."""
        from code_indexer.server.mcp.handlers import handle_edit_file
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as mock_crud:
            mock_crud.is_write_exception.return_value = False
            mock_crud.edit_file.return_value = {"success": True}
            with patch(
                "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
            ) as mock_arm_cls:
                mock_arm = MagicMock()
                mock_arm.get_activated_repo_path.return_value = "/fake/repo"
                mock_arm_cls.return_value = mock_arm
                with patch(
                    "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers.app_module"
                    ) as mock_app_module:
                        mock_app_module.app.state.golden_repos_dir = "/fake/golden"
                        with patch(
                            "code_indexer.server.mcp.handlers._is_write_mode_active",
                            return_value=False,
                        ):
                            handle_edit_file(
                                {
                                    "repository_alias": "my-repo",
                                    "file_path": "docs/guide.md",
                                    "old_string": "old",
                                    "new_string": "new",
                                    "content_hash": "abc123",
                                },
                                mock_user,
                            )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_handle_delete_file_calls_invalidator_for_md(self):
        """handle_delete_file must call wiki_cache_invalidator.invalidate_for_file_change."""
        from code_indexer.server.mcp.handlers import handle_delete_file
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as mock_crud:
            mock_crud.is_write_exception.return_value = False
            mock_crud.delete_file.return_value = {"success": True}
            with patch(
                "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
            ) as mock_arm_cls:
                mock_arm = MagicMock()
                mock_arm.get_activated_repo_path.return_value = "/fake/repo"
                mock_arm_cls.return_value = mock_arm
                with patch(
                    "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers.app_module"
                    ) as mock_app_module:
                        mock_app_module.app.state.golden_repos_dir = "/fake/golden"
                        with patch(
                            "code_indexer.server.mcp.handlers._is_write_mode_active",
                            return_value=False,
                        ):
                            handle_delete_file(
                                {
                                    "repository_alias": "my-repo",
                                    "file_path": "docs/guide.md",
                                },
                                mock_user,
                            )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_invalidation_not_called_for_non_md_create(self):
        """handle_create_file must NOT call invalidator for .py files (AC5)."""
        from code_indexer.server.mcp.handlers import handle_create_file
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as mock_crud:
            mock_crud.is_write_exception.return_value = False
            mock_crud.create_file.return_value = {"success": True}
            with patch(
                "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
            ) as mock_arm_cls:
                mock_arm = MagicMock()
                mock_arm.get_activated_repo_path.return_value = "/fake/repo"
                mock_arm_cls.return_value = mock_arm
                with patch(
                    "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers.app_module"
                    ) as mock_app_module:
                        mock_app_module.app.state.golden_repos_dir = "/fake/golden"
                        with patch(
                            "code_indexer.server.mcp.handlers._is_write_mode_active",
                            return_value=False,
                        ):
                            handle_create_file(
                                {
                                    "repository_alias": "my-repo",
                                    "file_path": "src/module.py",
                                    "content": "print('hello')",
                                },
                                mock_user,
                            )

        mock_cache.invalidate_repo.assert_not_called()


class TestGitOperationInvalidationHookAC2:
    """AC2: Git operations trigger wiki cache invalidation."""

    def test_git_pull_calls_invalidator(self):
        """git_pull handler must call wiki_cache_invalidator after successful pull."""
        from code_indexer.server.mcp.handlers import git_pull
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_git:
            mock_git.pull_from_remote.return_value = {"success": True}
            git_pull(
                {"repository_alias": "my-repo"},
                mock_user,
            )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_git_branch_switch_calls_invalidator(self):
        """git_branch_switch handler must call wiki_cache_invalidator after successful switch."""
        from code_indexer.server.mcp.handlers import git_branch_switch
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_git:
            mock_git.git_branch_switch.return_value = {
                "success": True,
                "current_branch": "main",
            }
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ):
                git_branch_switch(
                    {"repository_alias": "my-repo", "branch_name": "main"},
                    mock_user,
                )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_git_reset_calls_invalidator_on_success(self):
        """git_reset handler must call wiki_cache_invalidator after successful reset."""
        from code_indexer.server.mcp.handlers import git_reset
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_git:
            mock_git.git_reset.return_value = {"success": True}
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ):
                git_reset(
                    {"repository_alias": "my-repo", "mode": "mixed"},
                    mock_user,
                )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_git_clean_calls_invalidator_on_success(self):
        """git_clean handler must call wiki_cache_invalidator after successful clean."""
        from code_indexer.server.mcp.handlers import git_clean
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_git:
            mock_git.git_clean.return_value = {"success": True}
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ):
                git_clean(
                    {
                        "repository_alias": "my-repo",
                        "confirmation_token": "abc",
                    },
                    mock_user,
                )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_git_merge_abort_calls_invalidator_on_success(self):
        """git_merge_abort handler must call wiki_cache_invalidator after successful abort."""
        from code_indexer.server.mcp.handlers import git_merge_abort
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_git:
            mock_git.git_merge_abort.return_value = {"success": True}
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ):
                git_merge_abort(
                    {"repository_alias": "my-repo"},
                    mock_user,
                )

        mock_cache.invalidate_repo.assert_called_with("my-repo")

    def test_git_checkout_file_calls_invalidator_on_success(self):
        """git_checkout_file handler must call wiki_cache_invalidator after successful checkout."""
        from code_indexer.server.mcp.handlers import git_checkout_file
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_git:
            mock_git.git_checkout_file.return_value = {"success": True}
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path",
                return_value=("/fake/repo", None),
            ):
                git_checkout_file(
                    {
                        "repository_alias": "my-repo",
                        "file_path": "docs/guide.md",
                    },
                    mock_user,
                )

        mock_cache.invalidate_repo.assert_called_with("my-repo")


class TestWriteModeExitInvalidationHookAC9:
    """AC9: Write-mode exit triggers wiki cache invalidation."""

    def test_handle_exit_write_mode_calls_invalidator(self):
        """handle_exit_write_mode must call wiki_cache_invalidator after triggering refresh."""
        from code_indexer.server.mcp.handlers import handle_exit_write_mode
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator

        mock_cache = MagicMock(spec=WikiCache)
        wiki_cache_invalidator.set_wiki_cache(mock_cache)

        mock_user = MagicMock()
        mock_user.username = "testuser"

        with patch(
            "code_indexer.server.services.file_crud_service.file_crud_service"
        ) as mock_crud:
            mock_crud.is_write_exception.return_value = True
            with patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden",
            ):
                with patch(
                    "code_indexer.server.mcp.handlers._write_mode_strip_global",
                    return_value="my-repo",
                ):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch(
                            "code_indexer.server.mcp.handlers._get_app_refresh_scheduler"
                        ) as mock_get_scheduler:
                            mock_scheduler = MagicMock()
                            mock_get_scheduler.return_value = mock_scheduler
                            with patch(
                                "code_indexer.server.mcp.handlers._write_mode_run_refresh"
                            ):
                                handle_exit_write_mode(
                                    {"repo_alias": "my-repo-global"},
                                    mock_user,
                                )

        mock_cache.invalidate_repo.assert_called_with("my-repo-global")
