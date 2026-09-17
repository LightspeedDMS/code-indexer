# ruff: noqa: F811
"""
Security regression tests for Bug #1891.

GET /api/repositories/{repo_id}/files with content=true joined a
caller-supplied `path` query parameter onto the activated repository's
root (`Path(repo.path) / path`) and read the result after only
exists()/is_file() checks -- no containment check. A `path` containing
parent-directory segments, an absolute path, or a symlink inside the
repository pointing outside it, let any authenticated user with an
activated repository read arbitrary files with the server process's
permissions.

The fix (src/code_indexer/server/routers/inline_repos_v2.py) confines the
resolved target to the repository root via the new shared helper
FileListingService.resolve_confined_path() (file_service.py) BEFORE any
exists()/is_file()/open() call, returning a generic 404 (identical to the
ordinary not-found response) on an escape attempt.

Uses the shared route-lookup / closure-patch helpers from
inline_routes_test_helpers.py (established pattern for this router) plus
a REAL temp directory on disk -- no mocks on the code path under test,
per CLAUDE.md Foundation #1.
"""

import os
from datetime import datetime, timezone

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    user_client,  # noqa: F401
)

OUTSIDE_MARKER = "SECRET_OUTSIDE_CONTENT_1891"
INSIDE_MARKER = "inside src/a.py content"


class _ContentActivatedRepoManager:
    """Stand-in for the route's `activated_repo_manager` closure variable,
    for the content=true branch: get_repository() returns a valid,
    non-composite repo dict and get_activated_repo_path() returns the
    real on-disk repo root under test."""

    def __init__(self, repo_root: str):
        self._repo_root = repo_root

    def get_repository(self, username: str, repo_id: str):
        now = datetime.now(timezone.utc).isoformat()
        return {
            "user_alias": repo_id,
            "activated_at": now,
            "last_accessed": now,
            "is_composite": False,
        }

    def get_activated_repo_path(self, username: str, repo_id: str) -> str:
        return self._repo_root


def _build_tree(tmp_path):
    """
    tmp_path/
      repo/                  <- repository root
        src/a.py             <- legitimate nested file
        link_outside -> ../outside.txt     (symlink escaping the repo)
        link_inside  -> src/a.py           (symlink staying inside the repo)
      outside.txt             <- file OUTSIDE the repository root
    """
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "a.py").write_text(INSIDE_MARKER)

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text(OUTSIDE_MARKER)

    os.symlink(outside_file, repo_root / "link_outside")
    os.symlink(repo_root / "src" / "a.py", repo_root / "link_inside")

    return repo_root, outside_file


def _get_files_content(user_client, repo_root, query_string: str):
    """Drive the route directly via a raw query string (so percent-encoded
    values, e.g. ..%2F, are sent on the wire exactly as written)."""
    handler = _find_route_handler("/api/repositories/{repo_id}/files", "GET")
    arm = _ContentActivatedRepoManager(str(repo_root))
    with _patch_closure(handler, "activated_repo_manager", arm):
        return user_client.get(f"/api/repositories/myrepo/files?{query_string}")


class TestParentDirectoryTraversalBlocked:
    def test_dot_dot_escape_returns_404_without_leaking_content(
        self, user_client, tmp_path
    ):
        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=../outside.txt"
        )

        assert response.status_code == 404, response.text
        assert OUTSIDE_MARKER not in response.text, (
            "parent-directory traversal must not leak the outside file's "
            f"content: {response.text}"
        )


class TestAbsolutePathEscapeBlocked:
    def test_absolute_path_returns_404_without_leaking_content(
        self, user_client, tmp_path
    ):
        repo_root, outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, f"content=true&path={outside_file}"
        )

        assert response.status_code == 404, response.text
        assert OUTSIDE_MARKER not in response.text, (
            "an absolute path escape must not leak the outside file's "
            f"content: {response.text}"
        )


class TestSymlinkEscapeBlocked:
    def test_symlink_to_outside_file_returns_404_without_leaking_content(
        self, user_client, tmp_path
    ):
        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=link_outside"
        )

        assert response.status_code == 404, response.text
        assert OUTSIDE_MARKER not in response.text, (
            "a symlink pointing outside the repository must not leak its "
            f"target's content: {response.text}"
        )


class TestUrlEncodedTraversalBlocked:
    def test_percent_encoded_dot_dot_returns_404_without_leaking_content(
        self, user_client, tmp_path
    ):
        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=..%2Foutside.txt"
        )

        assert response.status_code == 404, response.text
        assert OUTSIDE_MARKER not in response.text, (
            "a URL-encoded '..' traversal must not leak the outside "
            f"file's content: {response.text}"
        )


class TestLegitimatePathsStillWork:
    def test_nested_file_still_returns_content(self, user_client, tmp_path):
        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=src/a.py"
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_binary"] is False
        assert body["content"] == INSIDE_MARKER

    def test_symlink_to_inside_file_still_returns_content(self, user_client, tmp_path):
        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=link_inside"
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_binary"] is False
        assert body["content"] == INSIDE_MARKER


class TestMalformedPathsReturn404NotServerError:
    """Bug #1891 round 2 (S4): a malformed `path` (embedded NUL byte,
    symlink loop) must map to the SAME 404 the ordinary escape tests above
    already get -- not an uncaught-exception 500. Pre-fix,
    resolve_confined_path() let ValueError/RuntimeError from Path.resolve()
    propagate past the route's `except PermissionError: raise
    HTTPException(404)` untouched, so FastAPI's default handler turned it
    into a 500."""

    def test_percent_encoded_nul_byte_returns_404_not_500(self, user_client, tmp_path):
        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=src%2Fa.py%00evil"
        )

        assert response.status_code == 404, response.text
        assert OUTSIDE_MARKER not in response.text, response.text

    def test_symlink_loop_returns_404_not_500(self, user_client, tmp_path):
        repo_root, _outside_file = _build_tree(tmp_path)
        loop_a = repo_root / "loop_a"
        loop_b = repo_root / "loop_b"
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)

        response = _get_files_content(
            user_client, repo_root, "content=true&path=loop_a"
        )

        assert response.status_code == 404, response.text
        assert OUTSIDE_MARKER not in response.text, response.text


class TestDiscriminationProof:
    """Proves the tests above are discriminating: with the confinement
    check disabled (monkeypatched back to the pre-fix, unconfined
    behavior), the parent-directory-escape request DOES leak the outside
    file's content. This demonstrates the escape tests above would have
    caught Bug #1891 before the fix existed."""

    def test_escape_test_fails_without_confinement_fix(
        self, user_client, tmp_path, monkeypatch
    ):
        from pathlib import Path
        from code_indexer.server.routers import inline_repos_v2

        def _unconfined_resolve(repo_root: Path, relative_path: str) -> Path:
            # Pre-fix behavior: join and resolve, no containment check at all.
            return (Path(repo_root) / relative_path).resolve()

        # Bug #1891 D2: the route now calls the module-level
        # resolve_confined_path imported directly from
        # code_indexer.utils.path_confinement (no re-export shim on
        # FileListingService), so that is the name to patch.
        monkeypatch.setattr(
            inline_repos_v2, "resolve_confined_path", _unconfined_resolve
        )

        repo_root, _outside_file = _build_tree(tmp_path)
        response = _get_files_content(
            user_client, repo_root, "content=true&path=../outside.txt"
        )

        assert response.status_code == 200, (
            "with confinement disabled, the traversal request must "
            f"succeed (proving discrimination), got {response.text}"
        )
        assert OUTSIDE_MARKER in response.text, (
            "with confinement disabled, the outside file's content must "
            f"leak (proving discrimination), got {response.text}"
        )
