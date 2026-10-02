"""
Unit tests for FileListingService.list_directory_level / _collect_single_level.

The single-level fast path (os.scandir of one directory) returns the true immediate
children of a directory — honoring browse_directory's documented recursive=False
contract, which the full-tree walk violated (its `<dir>/*` pathspec pattern
over-matched into subdirectories). These tests assert the corrected single-level
result plus the fast path's own guards (traversal, excluded dirs, gitignore, missing
dir), and pin the old over-match so the behavior change is intentional.
"""

import pytest

from code_indexer.server.models.api_models import FileListQueryParams
from code_indexer.server.services.file_service import FileListingService


def _qp(path_pattern=None, language=None, sort_by="path", limit=500, page=1):
    return FileListQueryParams(
        page=page,
        limit=limit,
        path_pattern=path_pattern,
        language=language,
        sort_by=sort_by,
    )


class TestSingleLevelListing:
    @pytest.fixture
    def service(self):
        service = FileListingService.__new__(FileListingService)
        service.activated_repo_manager = None  # type: ignore[assignment]
        return service

    @pytest.fixture
    def temp_repo(self, tmp_path):
        """A repo with files at root, in code/, and nested under code/sub/."""
        (tmp_path / "README.md").write_text("# root")
        code = tmp_path / "code"
        code.mkdir()
        (code / "a.py").write_text("a = 1")
        (code / "b.java").write_text("class B {}")
        sub = code / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("deep = 1")  # nested — must NOT appear
        return tmp_path

    def test_returns_only_immediate_children(self, service, temp_repo):
        files = service._collect_single_level(str(temp_repo), "code")
        paths = sorted(f.path for f in files)
        assert paths == ["code/a.py", "code/b.java"]  # nested code/sub/deep.py excluded

    def test_fixes_recursive_false_overmatch(self, service, temp_repo):
        """The fast path fixes a doc-contract bug: recursive=False must return only
        immediate children, but the full-walk path filtered by the non-recursive
        `code/*` pattern over-matches into subdirectories (pathspec gitignore
        semantics — a matched directory pulls in its whole subtree). Assert the fast
        path returns true immediate children, and pin the old over-match so the
        intent of the change is explicit."""
        # Old behavior (documented here to make the fix intentional): `code/*` via
        # pathspec wrongly includes the nested code/sub/deep.py.
        full = service._collect_files(str(temp_repo))
        overmatched = sorted(
            f.path for f in service._apply_filters(full, _qp(path_pattern="code/*"))
        )
        assert "code/sub/deep.py" in overmatched  # the bug the fast path avoids

        # Fixed behavior: only the immediate children of code/.
        fast = sorted(
            f.path for f in service._collect_single_level(str(temp_repo), "code")
        )
        assert fast == ["code/a.py", "code/b.java"]

    def test_list_directory_level_language_filter_and_sort(self, service, temp_repo):
        resp = service.list_directory_level(
            str(temp_repo), "code", _qp(path_pattern="code/*", language="java")
        )
        assert [f.path for f in resp.files] == ["code/b.java"]

    def test_root_level(self, service, temp_repo):
        files = service._collect_single_level(str(temp_repo), "")
        assert sorted(f.path for f in files) == ["README.md"]  # not code/ contents

    def test_excludes_git_and_code_indexer(self, service, temp_repo):
        (temp_repo / ".git").mkdir()
        (temp_repo / ".git" / "HEAD").write_text("ref: x")
        ci = temp_repo / ".code-indexer"
        ci.mkdir()
        (ci / "meta.json").write_text("{}")
        # Browsing the excluded dir itself yields nothing.
        assert service._collect_single_level(str(temp_repo), ".git") == []
        assert service._collect_single_level(str(temp_repo), ".code-indexer") == []
        # And they never leak into a root browse.
        root = [f.path for f in service._collect_single_level(str(temp_repo), "")]
        assert all(".git" not in p and ".code-indexer" not in p for p in root)

    def test_respects_gitignore(self, service, temp_repo):
        (temp_repo / ".gitignore").write_text("*.log\n")
        (temp_repo / "code" / "debug.log").write_text("noise")
        paths = sorted(
            f.path for f in service._collect_single_level(str(temp_repo), "code")
        )
        assert "code/debug.log" not in paths
        assert paths == ["code/a.py", "code/b.java"]

    def test_traversal_outside_repo_returns_empty(self, service, temp_repo):
        assert service._collect_single_level(str(temp_repo), "../..") == []

    def test_missing_dir_returns_empty(self, service, temp_repo):
        assert service._collect_single_level(str(temp_repo), "nope") == []

    def test_path_to_a_file_returns_empty(self, service, temp_repo):
        # A file path (not a directory) is not browsable → empty, not an error.
        assert service._collect_single_level(str(temp_repo), "README.md") == []
