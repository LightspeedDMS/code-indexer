"""
Full-stack tests for browse_directory's recursive=False single-level
restriction (Bug #1886), driven through the REAL browse_directory() handler
and a REAL FileListingService against REAL temporary directories. The
filtering/sorting/pagination pipeline under test is entirely real; only the
auth object and the process-wide app_module singleton are substituted, the
same established pattern used throughout
tests/unit/server/mcp/test_browse_directory_filters.py.

Parametrized over both code paths the bug report calls out as needing
identical behavior:
    - "activated": repository_alias without a "-global" suffix, routed
      through FileListingService.list_files() (repo_id-based lookup via a
      real test-double ActivatedRepoManager).
    - "global": repository_alias with a "-global" suffix, routed through
      FileListingService.list_files_by_path() (direct path, via a patched
      _resolve_global_repo_target()).

Reproduces the exact reported symptom: browsing with recursive:false
must NOT return files from subdirectories.
"""

import json
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import browse_directory
from code_indexer.server.services.file_service import FileListingService


def _test_user():
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


class _FakeActivatedRepoManager:
    """Real (non-mock) test double routing repo_id -> a real filesystem path."""

    def __init__(self, path: str):
        self._path = path

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return self._path


def _browse(params: dict, mode: str, repo_path: str) -> dict:
    """Invoke the REAL browse_directory() handler, routed at repo_path via
    either the activated-repo or the global-repo code path, backed by a REAL
    FileListingService against the real filesystem."""
    real_service = FileListingService.__new__(FileListingService)
    alias = "myrepo-global" if mode == "global" else "myrepo"
    if mode == "activated":
        real_service.activated_repo_manager = _FakeActivatedRepoManager(  # type: ignore[assignment]
            repo_path
        )

    call_params = {"repository_alias": alias, **params}

    with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app_module:
        mock_app_module.file_service = real_service
        if mode == "global":
            with patch(
                "code_indexer.server.mcp.handlers.files._resolve_global_repo_target",
                return_value=(repo_path, None),
            ):
                result = browse_directory(call_params, _test_user())
        else:
            result = browse_directory(call_params, _test_user())

    parsed: dict = json.loads(result["content"][0]["text"])
    return parsed


def _file_paths(response: dict) -> list:
    return sorted(f["path"] for f in response["structure"]["files"])


@pytest.fixture(params=["activated", "global"])
def repo_mode(request):
    return request.param


class TestBrowseDirectoryNonRecursiveRootAndSubdir:
    """Reproduces the reported bug: recursive:false must exclude nested files."""

    def test_root_non_recursive_excludes_nested_files(self, tmp_path, repo_mode):
        (tmp_path / "root_a.py").write_text("a")
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "HtmlToPlainText.java").write_text("nested")

        response = _browse({"recursive": False}, repo_mode, str(tmp_path))

        assert response["success"] is True
        assert _file_paths(response) == ["root_a.py"], (
            "recursive:false at repo root must NOT include "
            "examples/HtmlToPlainText.java (the exact reported symptom)"
        )

    def test_subdir_non_recursive_excludes_nested_files(self, tmp_path, repo_mode):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "Nested.java").write_text("nested")

        response = _browse(
            {"path": "examples", "recursive": False}, repo_mode, str(tmp_path)
        )

        assert response["success"] is True
        assert _file_paths(response) == ["examples/Direct.java"]


class TestBrowseDirectoryNonRecursiveWithPatternAndLimit:
    """recursive:false combined with path_pattern and with limit/pagination."""

    def test_non_recursive_with_path_pattern(self, tmp_path, repo_mode):
        # A bare "*" path_pattern is the exact leaky shape the bug report
        # itself demonstrates: pathspec's gitwildmatch matches a trailing
        # bare "*" segment against ANY depth ("examples/*" matches
        # "examples/sub/X.java"), so this pattern alone does NOT anchor to
        # direct children -- only the depth filter does. A suffixed pattern
        # like "*.java" would anchor correctly on its own and would not be a
        # discriminating test for this fix.
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "Other.txt").write_text("other")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "NestedMatch.java").write_text("nested")

        response = _browse(
            {"path": "examples", "path_pattern": "*", "recursive": False},
            repo_mode,
            str(tmp_path),
        )

        assert response["success"] is True
        assert _file_paths(response) == [
            "examples/Direct.java",
            "examples/Other.txt",
        ]

    def test_non_recursive_limit_does_not_starve_direct_children(
        self, tmp_path, repo_mode
    ):
        (tmp_path / "examples").mkdir()
        # Nested file sorts alphabetically FIRST.
        (tmp_path / "examples" / "aaa_sub").mkdir()
        (tmp_path / "examples" / "aaa_sub" / "nested.java").write_text("nested")
        # Direct child sorts LAST.
        (tmp_path / "examples" / "zdirect.java").write_text("direct")

        response = _browse(
            {"path": "examples", "recursive": False, "limit": 1},
            repo_mode,
            str(tmp_path),
        )

        assert response["success"] is True
        assert _file_paths(response) == ["examples/zdirect.java"], (
            "the depth restriction must be applied before limit truncation, "
            "or the earlier-sorting nested file starves the real direct child"
        )


class TestBrowseDirectoryRecursiveUnchanged:
    """recursive:true (default) must be byte-for-byte unchanged: nested
    files remain included."""

    def test_recursive_default_includes_nested_files(self, tmp_path, repo_mode):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "Direct.java").write_text("direct")
        (tmp_path / "examples" / "sub").mkdir()
        (tmp_path / "examples" / "sub" / "Nested.java").write_text("nested")

        response = _browse({"path": "examples"}, repo_mode, str(tmp_path))

        assert response["success"] is True
        assert _file_paths(response) == [
            "examples/Direct.java",
            "examples/sub/Nested.java",
        ]

    def test_recursive_explicit_true_includes_nested_files(self, tmp_path, repo_mode):
        (tmp_path / "root_a.py").write_text("a")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested_b.py").write_text("b")

        response = _browse({"recursive": True}, repo_mode, str(tmp_path))

        assert response["success"] is True
        assert _file_paths(response) == ["root_a.py", "sub/nested_b.py"]
