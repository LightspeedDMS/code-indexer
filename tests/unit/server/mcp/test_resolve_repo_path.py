"""
Unit tests for _resolve_repo_path alias JSON resolution (Bug #340).

Verifies that _resolve_repo_path consults the alias JSON target_path
(via AliasManager) as the highest-priority resolution step, before falling
back to the git-centric resolution chain.

Also verifies:
- Error message fix in handle_regex_search (AC5)
- handle_directory_tree uses alias resolution for both global and non-global (AC3)

Tests:
1. Returns alias JSON target_path when alias exists and directory exists
2. Tries -global suffix for non-global identifier (e.g. 'cidx-meta' -> 'cidx-meta-global')
3. Falls back to existing chain when no alias exists
4. Falls back when alias path directory does not exist
5. Error message in handle_regex_search uses actual repository_alias (not hardcoded '.*')
6. handle_directory_tree resolves global repo via alias JSON
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestResolveRepoPathAliasJson:
    """Tests for alias JSON resolution in _resolve_repo_path (AC1, AC2, AC3, AC4)."""

    def test_returns_alias_json_path_when_alias_exists_and_dir_exists(
        self, tmp_path
    ):
        """AC1: alias JSON target_path is returned as highest-priority resolution."""
        from code_indexer.server.mcp.handlers import _resolve_repo_path

        # Create a real alias JSON file and target directory
        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)
        target_dir = tmp_path / "versioned" / "my-repo-global" / "v_123"
        target_dir.mkdir(parents=True)

        alias_data = {
            "target_path": str(target_dir),
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_refresh": "2026-01-01T00:00:00+00:00",
            "repo_name": "my-repo",
        }
        alias_file = aliases_dir / "my-repo-global.json"
        alias_file.write_text(json.dumps(alias_data))

        golden_repos_dir = str(tmp_path / "golden-repos")
        result = _resolve_repo_path("my-repo-global", golden_repos_dir)

        assert result == str(target_dir)

    def test_tries_global_suffix_for_non_global_identifier(self, tmp_path):
        """AC2: For 'cidx-meta', tries 'cidx-meta-global' alias as fallback."""
        from code_indexer.server.mcp.handlers import _resolve_repo_path

        # Create alias for 'cidx-meta-global' (not 'cidx-meta')
        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)
        target_dir = tmp_path / "local-repos" / "cidx-meta"
        target_dir.mkdir(parents=True)

        alias_data = {
            "target_path": str(target_dir),
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_refresh": "2026-01-01T00:00:00+00:00",
            "repo_name": "cidx-meta",
        }
        alias_file = aliases_dir / "cidx-meta-global.json"
        alias_file.write_text(json.dumps(alias_data))

        golden_repos_dir = str(tmp_path / "golden-repos")
        # Pass non-global identifier 'cidx-meta' — should find 'cidx-meta-global' alias
        result = _resolve_repo_path("cidx-meta", golden_repos_dir)

        assert result == str(target_dir)

    def test_falls_back_to_existing_chain_when_no_alias_exists(self, tmp_path):
        """AC4 partial: falls back to git-centric chain when no alias file exists."""
        from code_indexer.server.mcp.handlers import _resolve_repo_path

        # Create aliases dir but no alias file
        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)

        # Create a git repo at the golden-repos/my-git-repo location (Try 2 in chain)
        git_repo = tmp_path / "golden-repos" / "my-git-repo"
        git_repo.mkdir(parents=True)
        (git_repo / ".git").mkdir()

        golden_repos_dir = str(tmp_path / "golden-repos")

        # Mock the registry so it returns a repo entry pointing here
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = {
            "alias_name": "my-git-repo-global",
            "index_path": str(git_repo),
        }

        with patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            return_value=mock_registry,
        ):
            result = _resolve_repo_path("my-git-repo-global", golden_repos_dir)

        # Should have found it via the index_path in the git-centric chain (Try 1)
        assert result == str(git_repo)

    def test_falls_back_when_alias_path_directory_does_not_exist(self, tmp_path):
        """AC4: If alias target_path directory missing, falls back to git chain."""
        from code_indexer.server.mcp.handlers import _resolve_repo_path

        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)

        # Alias points to a non-existent directory
        missing_dir = tmp_path / "versioned" / "ghost" / "v_999"
        # Do NOT create missing_dir

        alias_data = {
            "target_path": str(missing_dir),
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_refresh": "2026-01-01T00:00:00+00:00",
            "repo_name": "ghost-repo",
        }
        alias_file = aliases_dir / "ghost-repo-global.json"
        alias_file.write_text(json.dumps(alias_data))

        # No git repo in any fallback location either — expect None
        golden_repos_dir = str(tmp_path / "golden-repos")

        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = {
            "alias_name": "ghost-repo-global",
            "index_path": str(missing_dir),  # Also points nowhere
        }

        with patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            return_value=mock_registry,
        ):
            result = _resolve_repo_path("ghost-repo-global", golden_repos_dir)

        # Alias dir doesn't exist -> falls through to git chain -> nothing -> None
        assert result is None

    def test_alias_takes_priority_over_stale_index_path(self, tmp_path):
        """AC1: Alias JSON beats stale index_path from registry."""
        from code_indexer.server.mcp.handlers import _resolve_repo_path

        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)

        # Alias points to versioned snapshot (the correct current path)
        versioned_dir = tmp_path / ".versioned" / "my-repo" / "v_200"
        versioned_dir.mkdir(parents=True)

        # Stale index_path also exists with .git (the OLD path)
        stale_dir = tmp_path / "golden-repos" / "my-repo"
        stale_dir.mkdir(parents=True)
        (stale_dir / ".git").mkdir()

        alias_data = {
            "target_path": str(versioned_dir),
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_refresh": "2026-02-01T00:00:00+00:00",
            "repo_name": "my-repo",
        }
        alias_file = aliases_dir / "my-repo-global.json"
        alias_file.write_text(json.dumps(alias_data))

        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = {
            "alias_name": "my-repo-global",
            "index_path": str(stale_dir),
        }

        golden_repos_dir = str(tmp_path / "golden-repos")
        with patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            return_value=mock_registry,
        ):
            result = _resolve_repo_path("my-repo-global", golden_repos_dir)

        # Alias JSON path (versioned snapshot) must win over stale index_path
        assert result == str(versioned_dir)
        assert result != str(stale_dir)


class TestRegexSearchErrorMessage:
    """Tests for handle_regex_search error message fix (AC5)."""

    def test_not_found_error_uses_actual_repository_alias(self):
        """AC5: Error message uses repository_alias variable, not hardcoded '.*'."""
        from code_indexer.server.mcp.handlers import handle_regex_search

        fake_user = MagicMock()
        fake_user.username = "testuser"
        fake_user.role = "admin"

        # Provide a repo alias that will not be found
        args = {
            "repository_alias": "nonexistent-repo-global",
            "pattern": "some_pattern",
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value="/nonexistent/dir",
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=None,
        ):
            response = asyncio.get_event_loop().run_until_complete(
                handle_regex_search(args, fake_user)
            )

        # Parse the MCP response
        import json as _json

        content = response.get("content", [{}])
        if isinstance(content, list) and content:
            text = content[0].get("text", "{}")
            result = _json.loads(text)
        else:
            result = response

        assert result.get("success") is False
        error_msg = result.get("error", "")
        # Must NOT contain the hardcoded '.*' bug
        assert "'.*'" not in error_msg
        # Must contain the actual alias name
        assert "nonexistent-repo-global" in error_msg


class TestHandleDirectoryTreeAliasResolution:
    """Tests for handle_directory_tree alias JSON resolution (AC3, AC6)."""

    def test_directory_tree_global_repo_resolved_via_alias_json(self, tmp_path):
        """AC3/AC6: handle_directory_tree for a global repo uses AliasManager."""
        from code_indexer.server.mcp.handlers import handle_directory_tree

        # Create a real directory to serve as the alias target
        repo_content_dir = tmp_path / "versioned" / "test-repo" / "v_100"
        repo_content_dir.mkdir(parents=True)
        # Create a file so the tree has something to show
        (repo_content_dir / "README.md").write_text("# Test")

        # Create the aliases directory so _resolve_repo_path's alias resolution kicks in
        (tmp_path / "golden-repos" / "aliases").mkdir(parents=True)

        fake_user = MagicMock()
        fake_user.username = "testuser"
        fake_user.role = "admin"

        args = {"repository_alias": "test-repo-global"}

        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "test-repo-global", "index_path": str(repo_content_dir)}
        ]
        mock_registry.get_global_repo.return_value = {
            "alias_name": "test-repo-global",
            "index_path": str(repo_content_dir),
            "repo_url": "git@github.com:org/test-repo.git",
        }

        mock_alias_manager = MagicMock()
        mock_alias_manager.read_alias.return_value = str(repo_content_dir)

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path / "golden-repos"),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            return_value=mock_registry,
        ), patch(
            "code_indexer.server.mcp.handlers.AliasManager",
            return_value=mock_alias_manager,
        ):
            response = handle_directory_tree(args, fake_user)

        import json as _json

        content = response.get("content", [{}])
        if isinstance(content, list) and content:
            text = content[0].get("text", "{}")
            result = _json.loads(text)
        else:
            result = response

        assert result.get("success") is True
        mock_alias_manager.read_alias.assert_called_once_with("test-repo-global")

    def test_directory_tree_non_global_repo_resolved_via_resolve_repo_path(
        self, tmp_path
    ):
        """AC3: handle_directory_tree for non-global repo uses _resolve_repo_path."""
        from code_indexer.server.mcp.handlers import handle_directory_tree

        repo_dir = tmp_path / "my-local-repo"
        repo_dir.mkdir()
        (repo_dir / "file.py").write_text("print('hello')")

        fake_user = MagicMock()
        fake_user.username = "testuser"
        fake_user.role = "admin"

        args = {"repository_alias": "my-local-repo"}

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path / "golden-repos"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=str(repo_dir),
        ):
            response = handle_directory_tree(args, fake_user)

        import json as _json

        content = response.get("content", [{}])
        if isinstance(content, list) and content:
            text = content[0].get("text", "{}")
            result = _json.loads(text)
        else:
            result = response

        assert result.get("success") is True
