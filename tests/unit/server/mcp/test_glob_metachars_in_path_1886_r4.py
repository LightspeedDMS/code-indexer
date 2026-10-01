"""
Handler-level tests for Bug #1886 (round 4, item 1): glob metacharacters in
`path` are not escaped before being spliced into the gitignore-style
path_pattern built by browse_directory / list_files, so a real directory
whose name happens to contain "[", "]", "*", or "?" (most commonly a
Next.js/SvelteKit/Nuxt/Astro dynamic-route folder like `app/[slug]/`) is
silently mis-parsed as a glob: files inside it are lost and an unrelated
sibling directory that accidentally satisfies the glob is returned instead.

Driven through the REAL browse_directory()/list_files() handlers and a REAL
FileListingService against REAL temporary directories -- the same
established pattern as test_leading_slash_path_pattern_1886_r3.py (a test
double stands in only for the surrounding auth/app-module wiring, never for
the filtering logic under test).

Fully parametrized (handler x metachar-case x recursive x repo_mode) to
avoid duplicating near-identical test bodies.
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


def _bracket_tree(tmp_path) -> None:
    """Next.js-style dynamic route directory + a sibling that would
    accidentally satisfy the unescaped character-class glob "[slug]"
    (any single char s/l/u/g)."""
    (tmp_path / "app" / "[slug]" / "sub").mkdir(parents=True)
    _ = (tmp_path / "app" / "[slug]" / "page.tsx").write_text("page")
    _ = (tmp_path / "app" / "[slug]" / "sub" / "x.tsx").write_text("x")
    (tmp_path / "app" / "s").mkdir()
    _ = (tmp_path / "app" / "s" / "other.tsx").write_text("other")


def _star_tree(tmp_path) -> None:
    """A literal directory named "star*dir" + a sibling that would
    accidentally satisfy the unescaped glob "star*dir" (star matches any
    run of chars)."""
    (tmp_path / "star*dir" / "nested").mkdir(parents=True)
    _ = (tmp_path / "star*dir" / "a.py").write_text("a")
    _ = (tmp_path / "star*dir" / "nested" / "b.py").write_text("b")
    (tmp_path / "starXdir").mkdir()
    _ = (tmp_path / "starXdir" / "other.py").write_text("other")


def _question_tree(tmp_path) -> None:
    """A literal directory named "quest?dir" + a sibling that would
    accidentally satisfy the unescaped glob "quest?dir" (? matches any
    single char)."""
    (tmp_path / "quest?dir" / "nested").mkdir(parents=True)
    _ = (tmp_path / "quest?dir" / "a.py").write_text("a")
    _ = (tmp_path / "quest?dir" / "nested" / "b.py").write_text("b")
    (tmp_path / "questXdir").mkdir()
    _ = (tmp_path / "questXdir" / "other.py").write_text("other")


# (path, tree_builder, expected files recursive=True, expected files recursive=False)
_METACHAR_CASES = [
    pytest.param(
        "app/[slug]",
        _bracket_tree,
        ["app/[slug]/page.tsx", "app/[slug]/sub/x.tsx"],
        ["app/[slug]/page.tsx"],
        id="bracket",
    ),
    pytest.param(
        "star*dir",
        _star_tree,
        ["star*dir/a.py", "star*dir/nested/b.py"],
        ["star*dir/a.py"],
        id="star",
    ),
    pytest.param(
        "quest?dir",
        _question_tree,
        ["quest?dir/a.py", "quest?dir/nested/b.py"],
        ["quest?dir/a.py"],
        id="question_mark",
    ),
]

# (handler, response-path-extractor)
_HANDLERS = [
    pytest.param(browse_directory, _browse_file_paths, id="browse_directory"),
    pytest.param(list_files, _list_file_paths, id="list_files"),
]


@pytest.mark.parametrize("handler,extract_paths", _HANDLERS)
@pytest.mark.parametrize(
    "path,tree_builder,expected_recursive,expected_non_recursive", _METACHAR_CASES
)
class TestGlobMetacharsInPath:
    """Bug #1886 (R4): a literal directory name containing gitwildmatch
    metacharacters must be matched literally, both for the full-subtree
    (recursive:true) and direct-children-only (recursive:false) cases, on
    both browse_directory and list_files, for both activated and global
    repos (repo_mode fixture)."""

    def test_recursive_true_returns_full_subtree_no_foreign_files(
        self,
        tmp_path,
        repo_mode,
        handler,
        extract_paths,
        path,
        tree_builder,
        expected_recursive,
        expected_non_recursive,
    ):
        tree_builder(tmp_path)
        response = _call(
            handler, {"path": path, "recursive": True}, repo_mode, str(tmp_path)
        )
        assert response["success"] is True
        assert extract_paths(response) == expected_recursive, (
            f"path={path!r} recursive=True must return exactly its own "
            f"subtree, never a foreign sibling, got {response}"
        )

    def test_recursive_false_returns_direct_children_only(
        self,
        tmp_path,
        repo_mode,
        handler,
        extract_paths,
        path,
        tree_builder,
        expected_recursive,
        expected_non_recursive,
    ):
        tree_builder(tmp_path)
        response = _call(
            handler, {"path": path, "recursive": False}, repo_mode, str(tmp_path)
        )
        assert response["success"] is True
        assert extract_paths(response) == expected_non_recursive, (
            f"path={path!r} recursive=False must return only direct "
            f"children, got {response}"
        )
