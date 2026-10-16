# ruff: noqa: F811
"""Repository paths with a ``.git`` segment are refused on every file-reading
and path-taking front door, with the same response as a nonexistent path,
and listings never show ``.git`` entries.

Driven through a real app (repo_url_userinfo_env): the golden clone and the
user's activation each have a real ``origin`` remote whose URL carries
userinfo in ``.git/config``. Every response is checked for the secret, and
each refused ``.git`` path's full response is compared with the response
for a path that does not exist.

Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import pytest
from fastapi.testclient import TestClient

from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    GLOBAL_ALIAS,
    SECRET,
    USER,
    USER_ACTIVATION,
    activate_for_user,
    app,
    assert_no_userinfo,
    client,
    get,
    golden_clone_path,
    store_userinfo_origin,
    mcp_call,
)

# Spellings of a path inside the repository's ``.git`` directory.
GIT_PATHS = [
    ".git/config",
    "./.git/config",
    "docs/../.git/config",
    ".git",
    ".git/",
]
MISSING_PATH = "missing-dir/config"
MISSING_DIR = "missing-dir"


@pytest.fixture
def activation(app: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    path = activate_for_user(app, monkeypatch)
    # Activation stores no credential; reproduce a clone that predates
    # run-time credentials so the .git denial is tested against a real one.
    store_userinfo_origin(path)
    return USER_ACTIVATION


def test_seeded_clones_carry_the_userinfo_origin(app: Any, activation: str) -> None:
    """The layout under test: both clones hold the credential in .git/config."""
    assert SECRET in (golden_clone_path(app) / ".git" / "config").read_text()


def _masked(text: str, requested: str) -> str:
    return text.replace(json.dumps(requested)[1:-1], "<path>").replace(
        requested, "<path>"
    )


def _assert_same_as_missing(
    call: Callable[[str], str], git_path: str, missing: str
) -> None:
    refused = call(git_path)
    assert_no_userinfo(refused)
    assert _masked(refused, git_path) == _masked(call(missing), missing)


def _mcp_body(
    client: TestClient, app: Any, tool: str, arguments: Dict[str, Any]
) -> str:
    return json.dumps(mcp_call(client, app, USER, tool, arguments))


# ------------------------------------------------------------------ MCP reads


def _repos(activation: str) -> List[str]:
    return [GLOBAL_ALIAS, activation]


@pytest.mark.parametrize("git_path", GIT_PATHS)
def test_get_file_content_refuses_git_paths_like_missing_files(
    client: TestClient, app: Any, activation: str, git_path: str
) -> None:
    for repo in _repos(activation):
        _assert_same_as_missing(
            lambda p: _mcp_body(
                client,
                app,
                "get_file_content",
                {"repository_alias": repo, "file_path": p},
            ),
            git_path,
            MISSING_PATH,
        )


@pytest.mark.parametrize("git_path", [".git/config", ".git", "./.git"])
def test_regex_search_refuses_git_paths_like_missing_paths(
    client: TestClient, app: Any, activation: str, git_path: str
) -> None:
    missing = MISSING_PATH if git_path.endswith("config") else MISSING_DIR
    for repo in _repos(activation):
        _assert_same_as_missing(
            lambda p: _mcp_body(
                client,
                app,
                "regex_search",
                {"repository_alias": repo, "pattern": "example", "path": p},
            ),
            git_path,
            missing,
        )


def test_regex_search_include_patterns_never_match_git_files(
    client: TestClient, app: Any, activation: str
) -> None:
    for repo in _repos(activation):
        body = _mcp_body(
            client,
            app,
            "regex_search",
            {
                "repository_alias": repo,
                "pattern": "example",
                "include_patterns": [".git/**", "**/config", "config"],
            },
        )
        assert_no_userinfo(body)
        assert ".git" not in body, body


@pytest.mark.parametrize("tool", ["list_files", "browse_directory", "directory_tree"])
def test_listings_refuse_git_paths_like_missing_paths(
    client: TestClient, app: Any, activation: str, tool: str
) -> None:
    # directory_tree resolves golden/global repositories only.
    repos = [GLOBAL_ALIAS] if tool == "directory_tree" else _repos(activation)
    for repo in repos:
        _assert_same_as_missing(
            lambda p: _mcp_body(
                client, app, tool, {"repository_alias": repo, "path": p}
            ),
            ".git",
            MISSING_DIR,
        )


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("list_files", {}),
        ("browse_directory", {"recursive": True}),
        ("directory_tree", {}),
        ("directory_tree", {"include_hidden": True}),
    ],
)
def test_listings_show_no_git_entries(
    client: TestClient,
    app: Any,
    activation: str,
    tool: str,
    arguments: Dict[str, Any],
) -> None:
    # directory_tree resolves golden/global repositories only.
    repos = [GLOBAL_ALIAS] if tool == "directory_tree" else _repos(activation)
    for repo in repos:
        body = _mcp_body(client, app, tool, {"repository_alias": repo, **arguments})
        assert "README.md" in body, body
        assert ".git" not in body, body


def test_xray_dump_ast_refuses_git_paths_like_missing_files(
    client: TestClient, app: Any, activation: str
) -> None:
    for repo in _repos(activation):
        _assert_same_as_missing(
            lambda p: _mcp_body(
                client,
                app,
                "xray_dump_ast",
                {"repository_alias": repo, "file_path": p},
            ),
            ".git/config",
            MISSING_PATH,
        )


@pytest.mark.parametrize("tool", ["git_file_at_revision", "git_blame"])
def test_git_revision_reads_refuse_git_paths_like_missing_files(
    client: TestClient, app: Any, activation: str, tool: str
) -> None:
    _assert_same_as_missing(
        lambda p: _mcp_body(
            client,
            app,
            tool,
            {"repository_alias": activation, "path": p, "revision": "HEAD"},
        ),
        ".git/config",
        MISSING_PATH,
    )


# ----------------------------------------------------------------------- REST


def test_rest_v2_file_content_refuses_git_paths_like_missing_files(
    client: TestClient, app: Any, activation: str
) -> None:
    def call(p: str) -> str:
        r = get(
            client,
            app,
            USER,
            f"/api/repositories/{activation}/files",
            params={"path": p, "content": "true"},
        )
        return f"{r.status_code} {r.text}"

    for git_path in GIT_PATHS[:3]:
        _assert_same_as_missing(call, git_path, MISSING_PATH)


def test_rest_repo_file_listing_refuses_and_hides_git(
    client: TestClient, app: Any, activation: str
) -> None:
    def call(p: str) -> str:
        r = get(client, app, USER, f"/api/repos/{activation}/files", params={"path": p})
        return f"{r.status_code} {r.text}"

    for git_path in [".git", "./.git", "docs/../.git"]:
        _assert_same_as_missing(call, git_path, MISSING_DIR)
    for params in ({}, {"recursive": "true"}):
        listing = get(
            client, app, USER, f"/api/repos/{activation}/files", params=params
        )
        assert listing.status_code == 200, listing.text
        assert "README.md" in listing.text, listing.text
        assert ".git" not in listing.text, listing.text


def test_rest_git_cat_refuses_git_paths_like_missing_files(
    client: TestClient, app: Any, activation: str
) -> None:
    def call(p: str) -> str:
        r = get(
            client,
            app,
            USER,
            f"/api/v1/repos/{activation}/git/cat",
            params={"path": p},
        )
        return f"{r.status_code} {r.text}"

    _assert_same_as_missing(call, ".git/config", MISSING_PATH)
