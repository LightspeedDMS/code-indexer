"""
Unit tests for Bug #336 Finding 1: handle_directory_tree on cidx-meta filters
unauthorized repo filenames for non-admin users.

Security fix: directory_tree was leaking all filenames (including repo-specific .md
files) to users who shouldn't see them, bypassing the filtering applied by
list_files, get_file_content, and browse_directory.
"""

from unittest.mock import MagicMock, patch

from code_indexer.global_repos.directory_explorer import DirectoryTreeResult, TreeNode
from code_indexer.server.mcp.handlers import handle_directory_tree

from .conftest import extract_mcp_data


def _make_tree_result(filenames: list) -> DirectoryTreeResult:
    """Build a DirectoryTreeResult with the given flat file list under root."""
    children = [
        TreeNode(name=name, path=name, is_directory=False)
        for name in filenames
    ]
    root = TreeNode(
        name="cidx-meta",
        path="",
        is_directory=True,
        children=children,
    )
    # Build a simple tree_string matching the format _format_tree_string produces
    lines = ["cidx-meta/"]
    for i, name in enumerate(filenames):
        connector = "+--" if i == len(filenames) - 1 else "|--"
        lines.append(f"{connector} {name}")
    tree_string = "\n".join(lines)
    return DirectoryTreeResult(
        root=root,
        tree_string=tree_string,
        total_directories=0,
        total_files=len(filenames),
        max_depth_reached=False,
        root_path="/fake/golden-repos/cidx-meta",
    )


_ALL_FILES = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]


class TestDirectoryTreeCidxMetaAccessFiltering:
    """Finding 1: handle_directory_tree on cidx-meta is filtered for non-admin users."""

    def _call_handle(self, alias, user, access_svc):
        """Helper: call handle_directory_tree with all infra mocked."""
        fake_tree = _make_tree_result(_ALL_FILES)
        mock_explorer = MagicMock()
        mock_explorer.generate_tree.return_value = fake_tree
        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value="/fake/golden-repos",
        ):
            with patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value="/fake/golden-repos/cidx-meta",
            ):
                with patch(
                    "code_indexer.global_repos.directory_explorer.DirectoryExplorerService",
                    return_value=mock_explorer,
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers._get_access_filtering_service",
                        return_value=access_svc,
                    ):
                        return handle_directory_tree(
                            {"repository_alias": alias}, user
                        )

    def test_regular_user_sees_no_repo_files_in_tree(
        self, regular_user, access_filtering_service
    ):
        """
        Finding 1: regular_user (cidx-meta only) calls directory_tree on cidx-meta.
        repo-a.md, repo-b.md, repo-c.md must not appear in children or tree_string.
        README.md is not a repo-specific file and must be visible.
        """
        result = self._call_handle("cidx-meta", regular_user, access_filtering_service)

        data = extract_mcp_data(result)
        assert data["success"] is True

        # Check root children names
        child_names = [c["name"] for c in data["root"]["children"]]
        assert "repo-a.md" not in child_names
        assert "repo-b.md" not in child_names
        assert "repo-c.md" not in child_names
        assert "README.md" in child_names

        # Check tree_string does not leak unauthorized names
        tree_str = data["tree_string"]
        assert "repo-a.md" not in tree_str
        assert "repo-b.md" not in tree_str
        assert "repo-c.md" not in tree_str
        assert "README.md" in tree_str

    def test_power_user_sees_only_accessible_files_in_tree(
        self, power_user, access_filtering_service
    ):
        """
        Finding 1: power_user (repo-a, repo-b) calls directory_tree on cidx-meta.
        repo-a.md and repo-b.md visible; repo-c.md hidden.
        """
        result = self._call_handle("cidx-meta", power_user, access_filtering_service)

        data = extract_mcp_data(result)
        assert data["success"] is True

        child_names = [c["name"] for c in data["root"]["children"]]
        assert "repo-a.md" in child_names
        assert "repo-b.md" in child_names
        assert "README.md" in child_names
        assert "repo-c.md" not in child_names

        tree_str = data["tree_string"]
        assert "repo-a.md" in tree_str
        assert "repo-b.md" in tree_str
        assert "README.md" in tree_str
        assert "repo-c.md" not in tree_str

    def test_no_access_filtering_service_returns_full_tree(self, regular_user):
        """If access_filtering_service is not configured, all files are returned."""
        result = self._call_handle("cidx-meta", regular_user, None)

        data = extract_mcp_data(result)
        assert data["success"] is True

        child_names = [c["name"] for c in data["root"]["children"]]
        assert len(child_names) == 4

        tree_str = data["tree_string"]
        for name in _ALL_FILES:
            assert name in tree_str

    def test_non_cidx_meta_repo_is_not_filtered(
        self, regular_user, access_filtering_service
    ):
        """Non-cidx-meta repos must not have filtering applied."""
        fake_tree = _make_tree_result(["src/auth.py", "src/secret.py"])
        mock_explorer = MagicMock()
        mock_explorer.generate_tree.return_value = fake_tree
        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value="/fake/golden-repos",
        ):
            with patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value="/fake/golden-repos/some-other-repo",
            ):
                with patch(
                    "code_indexer.global_repos.directory_explorer.DirectoryExplorerService",
                    return_value=mock_explorer,
                ):
                    with patch(
                        "code_indexer.server.mcp.handlers._get_access_filtering_service",
                        return_value=access_filtering_service,
                    ):
                        result = handle_directory_tree(
                            {"repository_alias": "some-other-repo"}, regular_user
                        )

        data = extract_mcp_data(result)
        assert data["success"] is True
        child_names = [c["name"] for c in data["root"]["children"]]
        assert len(child_names) == 2
