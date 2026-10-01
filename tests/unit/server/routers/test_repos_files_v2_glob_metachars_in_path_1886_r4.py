# ruff: noqa: F811
"""
REST route-level tests for Bug #1886 (round 4, item 1): glob metacharacters
in `path` are not escaped before GET /api/repositories/{repo_id}/files
splices it into the gitignore-style pattern it builds
(_build_non_composite_path_pattern), so a real directory whose name happens
to contain "[", "]", "*", or "?" (most commonly a Next.js/SvelteKit/Nuxt/
Astro dynamic-route folder like `app/[slug]/`) is silently mis-parsed as a
glob: files inside it are lost and an unrelated sibling directory that
accidentally satisfies the glob is returned instead.

Uses the shared route-lookup / closure-patch helpers from
inline_routes_test_helpers.py (the established pattern for this router),
plus a REAL FileListingService against a REAL temp directory -- no mocks on
the code path actually being exercised, following Foundation #1.
"""

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    user_client,  # noqa: F401
)
from unittest.mock import patch

from code_indexer.server.services.file_service import FileListingService


class _FakeActivatedRepoManager:
    """Non-composite stand-in for the route's `activated_repo_manager`
    closure variable -- get_repository() returning None routes the
    handler past the composite-repo branch into the regular listing path
    (the one under test here) without raising."""

    def get_repository(self, username: str, repo_id: str):
        return None


def _real_file_service(repo_path: str) -> FileListingService:
    service = FileListingService.__new__(FileListingService)

    class _ARM:
        def get_activated_repo_path(self, username: str, user_alias: str) -> str:
            return repo_path

    service.activated_repo_manager = _ARM()  # type: ignore[assignment]
    return service


def _bracket_tree(tmp_path) -> None:
    (tmp_path / "app" / "[slug]" / "sub").mkdir(parents=True)
    _ = (tmp_path / "app" / "[slug]" / "page.tsx").write_text("page")
    _ = (tmp_path / "app" / "[slug]" / "sub" / "x.tsx").write_text("x")
    (tmp_path / "app" / "s").mkdir()
    _ = (tmp_path / "app" / "s" / "other.tsx").write_text("other")


def _star_tree(tmp_path) -> None:
    (tmp_path / "star*dir" / "nested").mkdir(parents=True)
    _ = (tmp_path / "star*dir" / "a.py").write_text("a")
    _ = (tmp_path / "star*dir" / "nested" / "b.py").write_text("b")
    (tmp_path / "starXdir").mkdir()
    _ = (tmp_path / "starXdir" / "other.py").write_text("other")


def _question_tree(tmp_path) -> None:
    (tmp_path / "quest?dir" / "nested").mkdir(parents=True)
    _ = (tmp_path / "quest?dir" / "a.py").write_text("a")
    _ = (tmp_path / "quest?dir" / "nested" / "b.py").write_text("b")
    (tmp_path / "questXdir").mkdir()
    _ = (tmp_path / "questXdir" / "other.py").write_text("other")


def _list_files(user_client, tmp_path, query: str = ""):
    handler = _find_route_handler("/api/repositories/{repo_id}/files", "GET")
    real_service = _real_file_service(str(tmp_path))
    with _patch_closure(handler, "activated_repo_manager", _FakeActivatedRepoManager()):
        with patch(
            "code_indexer.server.routers.inline_repos_v2.file_service", real_service
        ):
            return user_client.get(f"/api/repositories/myrepo/files{query}")


def _file_paths(response) -> list:
    return sorted(f["path"] for f in response.json()["files"])


class TestBracketDirectoryPath:
    def test_path_only_returns_subtree_no_foreign_files(self, user_client, tmp_path):
        _bracket_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=app%2F%5Bslug%5D")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "app/[slug]/page.tsx",
            "app/[slug]/sub/x.tsx",
        ], (
            "path=app/[slug] must return its own subtree and never the "
            f"foreign 'app/s/other.tsx', got {response.json()}"
        )

    def test_path_with_relative_pattern_returns_subtree_filtered(
        self, user_client, tmp_path
    ):
        _bracket_tree(tmp_path)
        response = _list_files(
            user_client, tmp_path, "?path=app%2F%5Bslug%5D&path_pattern=*.tsx"
        )

        assert response.status_code == 200
        assert _file_paths(response) == [
            "app/[slug]/page.tsx",
            "app/[slug]/sub/x.tsx",
        ]


class TestStarDirectoryPath:
    def test_path_only_returns_subtree_no_foreign_files(self, user_client, tmp_path):
        _star_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=star%2Adir")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "star*dir/a.py",
            "star*dir/nested/b.py",
        ], (
            "literal star-named directory must never match the foreign "
            f"'starXdir/other.py' sibling, got {response.json()}"
        )


class TestQuestionMarkDirectoryPath:
    def test_path_only_returns_subtree_no_foreign_files(self, user_client, tmp_path):
        _question_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=quest%3Fdir")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "quest?dir/a.py",
            "quest?dir/nested/b.py",
        ], (
            "literal question-mark-named directory must never match the "
            f"foreign 'questXdir/other.py' sibling, got {response.json()}"
        )
