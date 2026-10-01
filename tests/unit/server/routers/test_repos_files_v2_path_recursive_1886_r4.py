# ruff: noqa: F811
"""
Tests for GET /api/repositories/{repo_id}/files honoring `path` as a
subtree filter for non-composite repositories, and leaving `recursive`
composite-only (Bug #1886, R4 -- revised in round 3 after dual review of
round 2 found two regressions in the direct-children restriction it had
applied to non-composite repos: `?recursive=false&path_pattern=code/src/*.java`
returned [] instead of HEAD's match, and `?recursive=false&path_pattern=**/*.py`
matched only root files. Round 2 also silently broke every deployed
`cidx repos files` call: the CLI client (api_clients/repos_client.py) ALWAYS
sends recursive=false, so round 2 restricted every listing to root-only
direct children with zero directory entries.

DECISION (round 3, implemented exactly as specified): for non-composite
repositories `recursive` is dropped back to composite-only (its own
docstring already said so) and applies NO restriction whatsoever on the
non-composite branch -- the composite branch is reverted to be
byte-identical to HEAD (`recursive: bool = False`, no Optional[bool], no
bool() coercion). What non-composite REST gains over HEAD is `path` as a
subtree filter:
  - path absent/normalises to root -> exactly HEAD (only path_pattern, as
    before this whole bug).
  - path given, no path_pattern -> pattern "<path>/**/*" (full subtree).
  - path given + relative path_pattern (no "/", not starting "**") ->
    pattern "<path>/**/<path_pattern>".
  - path given + absolute path_pattern (contains "/" or starts with "**")
    -> the pattern alone, unmodified (same rule as MCP's
    _build_browse_path_pattern).
No direct_children_of is ever set by this route.

BACKWARD COMPATIBILITY IS MANDATORY: a request passing NEITHER `path` nor
`recursive` -- and, per this revision, a request passing ONLY
`recursive=false` (no `path`) -- must return exactly what HEAD returned:
the full repository listing, unrestricted by any depth/subtree filter.
This is the deployed-CLI case (repos_client.py always sends
recursive=false), verified here end-to-end (nested files present).

The composite-repository branch (routed through the REAL
_list_composite_files, not mocked) is untouched by this fix and must keep
working exactly as before (recursive: bool = False default, no Optional).

Uses the shared route-lookup / closure-patch helpers from
inline_routes_test_helpers.py (the established pattern for this router),
plus a REAL FileListingService against a REAL temp directory -- no mocks
on the code path actually being exercised, following Foundation #1.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    user_client,  # noqa: F401
)
from code_indexer.server.services.file_service import FileListingService


class _FakeActivatedRepoManager:
    """Non-composite stand-in for the route's `activated_repo_manager`
    closure variable -- get_repository() returning None routes the
    handler past the composite-repo branch into the regular listing path
    (the one under test here) without raising."""

    def get_repository(self, username: str, repo_id: str):
        return None


class _CompositeActivatedRepoManager:
    """Composite stand-in for the route's `activated_repo_manager`
    closure variable -- get_repository() returns a full, valid
    ActivatedRepository dict so ActivatedRepository.from_dict() (called by
    the route before invoking the REAL _list_composite_files) succeeds."""

    def __init__(self, repo_dict: dict):
        self._repo_dict = repo_dict

    def get_repository(self, username: str, repo_id: str):
        return self._repo_dict


def _real_file_service(repo_path: str) -> FileListingService:
    service = FileListingService.__new__(FileListingService)

    class _ARM:
        def get_activated_repo_path(self, username: str, user_alias: str) -> str:
            return repo_path

    service.activated_repo_manager = _ARM()  # type: ignore[assignment]
    return service


def _java_tree(tmp_path) -> None:
    (tmp_path / "code" / "src").mkdir(parents=True)
    _ = (tmp_path / "code" / "src" / "A.java").write_text("a")
    (tmp_path / "code" / "src" / "deep").mkdir()
    _ = (tmp_path / "code" / "src" / "deep" / "B.java").write_text("b")


def _mixed_tree(tmp_path) -> None:
    """A tree with a .py at root, a .py under src/, and a .py deeper still,
    so a path_pattern combined with a subtree path is discriminating."""
    _ = (tmp_path / "root.py").write_text("root")
    (tmp_path / "src").mkdir()
    _ = (tmp_path / "src" / "main.py").write_text("main")
    _ = (tmp_path / "src" / "notes.txt").write_text("notes")
    (tmp_path / "src" / "deep").mkdir()
    _ = (tmp_path / "src" / "deep" / "nested.py").write_text("nested")


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


class TestBackwardCompatNeitherParamGiven:
    """Request passing neither `path` nor `recursive` -> unchanged (full
    paginated listing), the mandatory backward-compat case."""

    def test_neither_param_returns_full_repo(self, user_client, tmp_path):
        _java_tree(tmp_path)
        response = _list_files(user_client, tmp_path)

        assert response.status_code == 200
        assert _file_paths(response) == [
            "code/src/A.java",
            "code/src/deep/B.java",
        ], "no path/recursive params must return the full repo, unchanged"


class TestRecursiveFalseAloneMatchesHead:
    """`recursive=false` alone (no `path`) is the deployed-CLI case
    (repos_client.py always sends it) -- it must return exactly HEAD's
    full, unrestricted listing, NOT a root-only direct-children view."""

    def test_recursive_false_no_path_returns_full_repo(self, user_client, tmp_path):
        _java_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?recursive=false")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "code/src/A.java",
            "code/src/deep/B.java",
        ], (
            "recursive=false with no path must match HEAD's unrestricted "
            f"listing (recursive is composite-only), got {response.json()}"
        )


class TestPathGivenNonComposite:
    """`path` given restricts to that subtree (all depths) -- `recursive`
    is ignored entirely on this branch."""

    def test_path_only_restricts_to_subtree(self, user_client, tmp_path):
        _java_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=code/src")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "code/src/A.java",
            "code/src/deep/B.java",
        ], f"path=code/src must return the whole subtree: {response.json()}"

    def test_path_with_recursive_false_still_returns_full_subtree(
        self, user_client, tmp_path
    ):
        _java_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=code/src&recursive=false")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "code/src/A.java",
            "code/src/deep/B.java",
        ], "recursive is composite-only and must not restrict this branch"

    def test_path_and_recursive_true_returns_full_subtree(self, user_client, tmp_path):
        _java_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=code&recursive=true")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "code/src/A.java",
            "code/src/deep/B.java",
        ]

    def test_path_with_relative_path_pattern_combines_subtree_and_pattern(
        self, user_client, tmp_path
    ):
        _mixed_tree(tmp_path)
        response = _list_files(user_client, tmp_path, "?path=src&path_pattern=*.py")

        assert response.status_code == 200
        assert _file_paths(response) == [
            "src/deep/nested.py",
            "src/main.py",
        ], (
            "path=src + relative path_pattern=*.py must match "
            f"src/**/*.py, got {response.json()}"
        )


class TestAbsolutePatternOverridesPath:
    """An absolute path_pattern (contains '/' or starts with '**') is used
    verbatim, overriding `path` entirely -- same rule as MCP's
    _build_browse_path_pattern. `recursive` still does not restrict."""

    def test_absolute_pattern_overrides_wrong_path_recursive_false(
        self, user_client, tmp_path
    ):
        _java_tree(tmp_path)
        response = _list_files(
            user_client,
            tmp_path,
            "?path=wrong&path_pattern=code/src/*.java&recursive=false",
        )

        assert response.status_code == 200
        assert _file_paths(response) == ["code/src/A.java"], (
            "absolute path_pattern must override `path` and match "
            f"regardless of recursive, got {response.json()}"
        )

    def test_double_star_pattern_matches_all_depths_recursive_false(
        self, user_client, tmp_path
    ):
        _mixed_tree(tmp_path)
        response = _list_files(
            user_client, tmp_path, "?recursive=false&path_pattern=**/*.py"
        )

        assert response.status_code == 200
        assert _file_paths(response) == [
            "root.py",
            "src/deep/nested.py",
            "src/main.py",
        ], (
            "**/*.py must match at every depth regardless of recursive, "
            f"got {response.json()}"
        )


def _build_composite_repo(tmp_path):
    """Real proxy-config composite repo on disk: one component ('sub1')
    with a top-level file and a nested file, so recursive vs non-recursive
    behavior is discriminating."""
    composite_path = tmp_path / "composite"
    sub1 = composite_path / "sub1"
    sub1.mkdir(parents=True)
    _ = (sub1 / "top.txt").write_text("top")
    (sub1 / "nested").mkdir()
    _ = (sub1 / "nested" / "deep.txt").write_text("deep")

    config_dir = composite_path / ".code-indexer"
    config_dir.mkdir()
    _ = (config_dir / "config.json").write_text(
        json.dumps({"proxy_mode": True, "discovered_repos": ["sub1"]})
    )
    return composite_path


def _composite_repo_dict(composite_path) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "user_alias": "myrepo",
        "username": "testuser",
        "path": str(composite_path),
        "activated_at": now,
        "last_accessed": now,
        "is_composite": True,
        "golden_repo_aliases": ["sub1"],
        "discovered_repos": ["sub1"],
    }


def _list_composite_files_via_route(user_client, tmp_path, query: str = ""):
    """Build a real composite repo on disk and drive it through the REAL
    (unmocked) route + _list_composite_files, mirroring _list_files'
    non-composite helper above."""
    composite_path = _build_composite_repo(tmp_path)
    handler = _find_route_handler("/api/repositories/{repo_id}/files", "GET")
    arm = _CompositeActivatedRepoManager(_composite_repo_dict(composite_path))
    with _patch_closure(handler, "activated_repo_manager", arm):
        return user_client.get(f"/api/repositories/myrepo/files{query}")


class TestCompositeRepositoryBranchUnchanged:
    """The composite-repo branch (REAL _list_composite_files, not mocked)
    is byte-identical to HEAD: `recursive: bool = False` default, no
    Optional[bool], no bool() coercion."""

    def test_composite_repo_recursive_unspecified_returns_direct_children_only(
        self, user_client, tmp_path
    ):
        response = _list_composite_files_via_route(user_client, tmp_path)

        assert response.status_code == 200
        paths = sorted(f["full_path"] for f in response.json()["files"])
        # Non-recursive composite listing includes directory entries too
        # (pre-existing _list_composite_files/_walk_directory behavior,
        # untouched by this fix) -- "sub1/nested/deep.txt" must NOT appear.
        assert paths == ["sub1/nested", "sub1/top.txt"], (
            "recursive unspecified must be treated as False for composite "
            f"repos (unchanged prior default), got {paths}"
        )

    def test_composite_repo_recursive_true_returns_nested(self, user_client, tmp_path):
        response = _list_composite_files_via_route(
            user_client, tmp_path, "?recursive=true"
        )

        assert response.status_code == 200
        paths = sorted(f["full_path"] for f in response.json()["files"])
        assert paths == ["sub1/nested/deep.txt", "sub1/top.txt"]
