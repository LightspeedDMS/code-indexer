"""
Full-stack tests for browse_directory's R1 and R2 regressions (Bug #1886,
round 2 of dual review), driven through the REAL browse_directory() handler
and a REAL FileListingService against REAL temporary directories -- the
same established pattern as
test_browse_directory_non_recursive_depth_1886.py.

R1 (P1, regression vs HEAD): an absolute path_pattern (contains "/" or
starts with "**") combined with recursive:false used to return [] instead
of HEAD's direct match, because direct_children_of was computed from
`path` even when the pattern overrides `path` entirely. The depth base
must come from the pattern's own literal directory prefix instead.

R2 (P1, regression vs HEAD): a leading slash in `path` (e.g. "/src") with
recursive:false used to return [] instead of HEAD's results, because only
the trailing slash was stripped before computing direct_children_of.

HEAD (pre-round-1, commit a5941e17) is the regression oracle: recursive:true
already tolerated a leading "/" in `path` (pathspec anchors it fine); only
recursive:false's depth-restriction machinery introduced the regression.
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
    def __init__(self, path: str):
        self._path = path

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return self._path


def _browse(params: dict, mode: str, repo_path: str) -> dict:
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


def _java_tree(tmp_path) -> None:
    (tmp_path / "code" / "src").mkdir(parents=True)
    _ = (tmp_path / "code" / "src" / "A.java").write_text("a")
    (tmp_path / "code" / "src" / "deep").mkdir()
    _ = (tmp_path / "code" / "src" / "deep" / "B.java").write_text("b")


def _src_tree(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    _ = (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "src" / "deep").mkdir()
    _ = (tmp_path / "src" / "deep" / "nested.py").write_text("y")


class TestR1AbsolutePatternNoPathOverride:
    """R1: absolute path_pattern + recursive:false must NOT return []."""

    def test_absolute_pattern_no_path_non_recursive(self, tmp_path, repo_mode):
        _java_tree(tmp_path)
        response = _browse(
            {"path_pattern": "code/src/*.java", "recursive": False},
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _file_paths(response) == ["code/src/A.java"], (
            "absolute path_pattern with recursive:false must return the "
            f"direct match like HEAD did, got {response['structure']['files']}"
        )

    def test_absolute_pattern_overrides_wrong_path_non_recursive(
        self, tmp_path, repo_mode
    ):
        _java_tree(tmp_path)
        response = _browse(
            {
                "path": "wrong/path",
                "path_pattern": "code/src/*.java",
                "recursive": False,
            },
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _file_paths(response) == ["code/src/A.java"]


class TestR1AbsolutePatternDepthDerivation:
    """R1: depth base must derive from the pattern, incl. trailing bare star."""

    def test_absolute_pattern_src_py_non_recursive(self, tmp_path, repo_mode):
        _src_tree(tmp_path)
        response = _browse(
            {"recursive": False, "path_pattern": "src/*.py"},
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _file_paths(response) == ["src/main.py"]

    def test_absolute_pattern_trailing_bare_star_excludes_nested(
        self, tmp_path, repo_mode
    ):
        """code/src/* (trailing bare star) must NOT return nested files."""
        _java_tree(tmp_path)
        response = _browse(
            {"path_pattern": "code/src/*", "recursive": False},
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _file_paths(response) == ["code/src/A.java"], (
            "code/src/* with recursive:false must not include the nested "
            f"code/src/deep/B.java, got {response['structure']['files']}"
        )


class TestR2LeadingSlashInPath:
    """R2: a leading slash in `path` must not zero out non-recursive results."""

    def test_leading_slash_path_non_recursive_returns_results(
        self, tmp_path, repo_mode
    ):
        _src_tree(tmp_path)
        response = _browse(
            {"path": "/src", "recursive": False}, repo_mode, str(tmp_path)
        )
        assert response["success"] is True
        assert _file_paths(response) == ["src/main.py"], (
            f"leading slash in path must not zero out results, "
            f"got {response['structure']['files']}"
        )

    def test_leading_slash_path_recursive_unaffected(self, tmp_path, repo_mode):
        _src_tree(tmp_path)
        response = _browse({"path": "/src"}, repo_mode, str(tmp_path))
        assert response["success"] is True
        assert _file_paths(response) == ["src/deep/nested.py", "src/main.py"]

    def test_leading_dot_slash_path_non_recursive_returns_results(
        self, tmp_path, repo_mode
    ):
        _src_tree(tmp_path)
        response = _browse(
            {"path": "./src/", "recursive": False}, repo_mode, str(tmp_path)
        )
        assert response["success"] is True
        assert _file_paths(response) == ["src/main.py"]
