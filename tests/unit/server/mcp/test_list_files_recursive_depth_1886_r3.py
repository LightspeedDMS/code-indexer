"""
Full-stack tests for MCP list_files()'s recursive=False single-level
restriction (Bug #1886, R3 -- same defect class as browse_directory's R1
fix, at a second front door), driven through the REAL list_files() handler
and a REAL FileListingService against REAL temporary directories -- the
same established pattern as
test_browse_directory_non_recursive_depth_1886.py.

list_files' own tool doc (list_files.md) documents recursive:false as
"uses * pattern (direct children only)", but nothing enforced that --
recursive:false returned the whole subtree, identical to the
browse_directory bug this issue's round 1 already fixed.
"""

import json
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import list_files
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


def _list(params: dict, mode: str, repo_path: str) -> dict:
    real_service = FileListingService.__new__(FileListingService)
    alias = "myrepo-global" if mode == "global" else "myrepo"
    if mode == "activated":
        # FileListingService.activated_repo_manager is typed as the real
        # ActivatedRepoManager; the fake here only implements the one
        # method list_files() actually calls, so mypy cannot verify
        # structural compatibility -- ignored deliberately, matching the
        # existing pattern in test_browse_directory_non_recursive_depth_1886.py.
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
                result = list_files(call_params, _test_user())
        else:
            result = list_files(call_params, _test_user())

    parsed: dict = json.loads(result["content"][0]["text"])
    return parsed


def _file_paths(response: dict) -> list:
    return sorted(f["path"] for f in response["files"])


@pytest.fixture(params=["activated", "global"])
def repo_mode(request):
    return request.param


def _java_tree(tmp_path) -> None:
    (tmp_path / "code" / "src").mkdir(parents=True)
    _ = (tmp_path / "code" / "src" / "A.java").write_text("a")
    (tmp_path / "code" / "src" / "deep").mkdir()
    _ = (tmp_path / "code" / "src" / "deep" / "B.java").write_text("b")


class TestListFilesNonRecursiveExcludesNested:
    """R3: recursive:false must return only direct children, at root and
    when scoped to a subdirectory."""

    def test_root_non_recursive_excludes_nested(self, tmp_path, repo_mode):
        _ = (tmp_path / "root_a.py").write_text("a")
        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "src").mkdir()
        _ = (tmp_path / "code" / "src" / "A.java").write_text("a")

        response = _list({"recursive": False}, repo_mode, str(tmp_path))

        assert response["success"] is True
        assert _file_paths(response) == ["root_a.py"], (
            "recursive:false at repo root must return direct children "
            f"only, got {response['files']}"
        )

    def test_subdir_non_recursive_excludes_deeper_nested(self, tmp_path, repo_mode):
        _java_tree(tmp_path)
        response = _list({"path": "code", "recursive": False}, repo_mode, str(tmp_path))

        assert response["success"] is True
        assert _file_paths(response) == [], (
            "recursive:false with path='code' must not return "
            f"code/src/deep/B.java (2 levels deep), got {response['files']}"
        )


class TestListFilesRecursiveUnchanged:
    """recursive:true (default) must be byte-for-byte unchanged."""

    def test_recursive_default_includes_nested_files(self, tmp_path, repo_mode):
        _java_tree(tmp_path)
        response = _list({}, repo_mode, str(tmp_path))

        assert response["success"] is True
        assert _file_paths(response) == [
            "code/src/A.java",
            "code/src/deep/B.java",
        ]
