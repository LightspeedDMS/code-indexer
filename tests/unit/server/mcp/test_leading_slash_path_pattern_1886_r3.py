"""
Handler-level tests for Bug #1886 (R3, item 1): a path_pattern that itself
starts with "/" (or "./") combined with recursive:false must NOT return []
-- driven through the REAL browse_directory() and list_files() handlers and
a REAL FileListingService against REAL temporary directories, the same
established pattern as
test_browse_directory_absolute_pattern_and_leading_slash_1886_r1_r2.py and
test_list_files_recursive_depth_1886_r3.py (a test double stands in only
for the surrounding auth/app-module wiring, never for the filtering logic
under test).

Root cause: literal_dir_prefix_of_pattern("/src/*.py") returned "/src"
(leading slash kept), which never equals a real file's parent dir "src"
(FileInfo.path is built via Path.relative_to(), never leading-slashed) --
so the direct_children_of depth filter excluded every real match.
"""

import json
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import browse_directory, list_files
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


def _call(handler, params: dict, mode: str, repo_path: str) -> dict:
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
                result = handler(call_params, _test_user())
        else:
            result = handler(call_params, _test_user())

    parsed: dict = json.loads(result["content"][0]["text"])
    return parsed


def _browse_file_paths(response: dict) -> list:
    return sorted(f["path"] for f in response["structure"]["files"])


def _list_file_paths(response: dict) -> list:
    return sorted(f["path"] for f in response["files"])


@pytest.fixture(params=["activated", "global"])
def repo_mode(request):
    return request.param


def _src_tree(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    _ = (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "src" / "deep").mkdir()
    _ = (tmp_path / "src" / "deep" / "nested.py").write_text("y")


def _java_tree(tmp_path) -> None:
    (tmp_path / "code" / "src").mkdir(parents=True)
    _ = (tmp_path / "code" / "src" / "A.java").write_text("a")
    (tmp_path / "code" / "src" / "deep").mkdir()
    _ = (tmp_path / "code" / "src" / "deep" / "B.java").write_text("b")


class TestBrowseDirectoryLeadingSlashPattern:
    def test_leading_slash_pattern_non_recursive_returns_direct_match(
        self, tmp_path, repo_mode
    ):
        _src_tree(tmp_path)
        response = _call(
            browse_directory,
            {"path_pattern": "/src/*.py", "recursive": False},
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _browse_file_paths(response) == ["src/main.py"], (
            "leading-slash path_pattern with recursive:false must return "
            f"the direct match, got {response['structure']['files']}"
        )

    def test_leading_slash_pattern_overrides_wrong_path_non_recursive(
        self, tmp_path, repo_mode
    ):
        _java_tree(tmp_path)
        response = _call(
            browse_directory,
            {
                "path": "wrong/path",
                "path_pattern": "/code/src/*.java",
                "recursive": False,
            },
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _browse_file_paths(response) == ["code/src/A.java"]


class TestListFilesLeadingSlashPattern:
    def test_leading_slash_pattern_non_recursive_returns_direct_match(
        self, tmp_path, repo_mode
    ):
        _src_tree(tmp_path)
        response = _call(
            list_files,
            {"path_pattern": "/src/*.py", "recursive": False},
            repo_mode,
            str(tmp_path),
        )
        assert response["success"] is True
        assert _list_file_paths(response) == ["src/main.py"], (
            "leading-slash path_pattern with recursive:false must return "
            f"the direct match, got {response['files']}"
        )
