"""
Unit tests for FileListingService's ``direct_children_of`` query filter (Bug #1886).

Bug #1886: browse_directory with recursive:false returned files from
subdirectories -- the tool's own schema documents recursive:false as
"returns only immediate children (single level)", but nothing in the
filtering pipeline actually enforced a depth restriction. These tests
exercise the depth-restriction mechanism directly against
FileListingService using REAL temporary directories (Foundation #1: no
mocks) -- the same real-filesystem pattern used in
test_file_service_path_pattern.py.

The mechanism is shared by both list_files() (activated repos) and
list_files_by_path() (global repos) via the common _apply_filters() step,
so exercising it here covers both call paths' underlying filtering logic.
"""

from code_indexer.server.services.file_service import FileListingService
from code_indexer.server.models.api_models import FileListQueryParams


class _FakeActivatedRepoManager:
    """Minimal test double for the activated-repo lookup (Bug #1650 pattern)."""

    def __init__(self, path: str):
        self._path = path

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return self._path


def _service() -> FileListingService:
    """Real FileListingService instance, bypassing __init__ (established pattern)."""
    service = FileListingService.__new__(FileListingService)
    service.activated_repo_manager = None  # type: ignore[assignment]
    return service


class TestDirectChildrenOfRestrictsToTopLevelFiles:
    """direct_children_of="" restricts results to repository-root files only."""

    def test_root_direct_children_excludes_nested_files(self, tmp_path):
        (tmp_path / "root_a.py").write_text("a")
        (tmp_path / "root_b.py").write_text("b")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested_c.py").write_text("c")

        query_params = FileListQueryParams(  # type: ignore[call-arg]
            page=1, limit=100, direct_children_of=""
        )
        result = _service().list_files_by_path(
            repo_path=str(tmp_path), query_params=query_params
        )

        file_paths = sorted(f.path for f in result.files)
        assert file_paths == ["root_a.py", "root_b.py"], (
            f"recursive=false at repo root must exclude sub/nested_c.py, got {file_paths}"
        )


class TestDirectChildrenOfRestrictsToSubdirectory:
    """direct_children_of="examples" restricts results to direct children of examples/."""

    def test_subdir_direct_children_excludes_nested_files(self, tmp_path):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "Nested.java").write_text("nested")

        query_params = FileListQueryParams(  # type: ignore[call-arg]
            page=1, limit=100, direct_children_of="examples"
        )
        result = _service().list_files_by_path(
            repo_path=str(tmp_path), query_params=query_params
        )

        file_paths = sorted(f.path for f in result.files)
        assert file_paths == ["examples/Direct.java"], (
            "recursive=false browsing examples/ must exclude examples/sub/Nested.java, "
            f"got {file_paths}"
        )


class TestDirectChildrenOfCombinedWithPathPattern:
    """direct_children_of combined with path_pattern narrows on BOTH axes."""

    def test_pattern_plus_depth_filter(self, tmp_path):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "Other.txt").write_text("other")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "NestedMatch.java").write_text("nested")

        query_params = FileListQueryParams(  # type: ignore[call-arg]
            page=1,
            limit=100,
            path_pattern="examples/**/*.java",
            direct_children_of="examples",
        )
        result = _service().list_files_by_path(
            repo_path=str(tmp_path), query_params=query_params
        )

        file_paths = sorted(f.path for f in result.files)
        assert file_paths == ["examples/Direct.java"], (
            "path_pattern matches .java at any depth under examples/, but "
            f"direct_children_of must still exclude the nested match, got {file_paths}"
        )


class TestDirectChildrenOfAppliedBeforePagination:
    """Depth filter must run BEFORE limit truncation (a page of nested files
    sorting alphabetically first must not starve real direct children)."""

    def test_limit_does_not_starve_direct_child_behind_nested_sort_order(
        self, tmp_path
    ):
        (tmp_path / "examples").mkdir()
        # Nested file sorts alphabetically FIRST ("aaa_sub/..." < "zdirect.java").
        (tmp_path / "examples" / "aaa_sub").mkdir()
        (tmp_path / "examples" / "aaa_sub" / "nested.java").write_text("nested")
        # Direct child sorts LAST.
        (tmp_path / "examples" / "zdirect.java").write_text("direct")

        query_params = FileListQueryParams(  # type: ignore[call-arg]
            page=1, limit=1, direct_children_of="examples"
        )
        result = _service().list_files_by_path(
            repo_path=str(tmp_path), query_params=query_params
        )

        file_paths = [f.path for f in result.files]
        assert file_paths == ["examples/zdirect.java"], (
            "with limit=1, the depth filter must run BEFORE pagination truncates "
            f"the list, or the alphabetically-earlier nested file starves the "
            f"real direct child; got {file_paths}"
        )


class TestDirectChildrenOfNoneLeavesRecursiveBehaviorUnchanged:
    """direct_children_of=None (the default/unset case) must be byte-for-byte
    unchanged -- nested files remain included, exactly like before this fix."""

    def test_unset_direct_children_of_includes_nested_files(self, tmp_path):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "Nested.java").write_text("nested")

        query_params = FileListQueryParams(page=1, limit=100)  # type: ignore[call-arg]
        result = _service().list_files_by_path(
            repo_path=str(tmp_path), query_params=query_params
        )

        file_paths = sorted(f.path for f in result.files)
        assert file_paths == ["examples/Direct.java", "examples/sub/Nested.java"], (
            f"recursive (default) must be unchanged: both files expected, got {file_paths}"
        )


class TestDirectChildrenOfActivatedRepoPath:
    """Sanity check that the depth filter also applies via list_files()
    (repo_id-based, activated-repo code path), not only list_files_by_path()."""

    def test_activated_repo_subdir_excludes_nested_files(self, tmp_path):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "Nested.java").write_text("nested")

        service = FileListingService.__new__(FileListingService)
        service.activated_repo_manager = _FakeActivatedRepoManager(str(tmp_path))  # type: ignore[assignment]

        query_params = FileListQueryParams(  # type: ignore[call-arg]
            page=1, limit=100, direct_children_of="examples"
        )
        result = service.list_files(
            repo_id="myrepo", username="testuser", query_params=query_params
        )

        file_paths = sorted(f.path for f in result.files)
        assert file_paths == ["examples/Direct.java"], (
            f"activated-repo list_files() must honor direct_children_of, got {file_paths}"
        )
