# ruff: noqa: F811
# (the shared fixtures imported below are pytest fixtures used as test
# parameters, which ruff reports as redefinitions)
"""Cleaning untracked files never removes the repository's own index.

Invariant: every git clean the server runs (REST ``/git/clean``, MCP
``git_clean``, and the pre-refresh clearing of a dirty repository) removes
the caller's untracked files but keeps cidx's own working paths -- the
``.code-indexer/`` index directory and ``.code-indexer-override.yaml`` --
with their contents, and never lists them as removed.

Every test runs a real ``git clean`` in a real git repository.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import git_write
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from code_indexer.server.services.git_state_manager import GitStateManager
from tests.unit.server.query.query_repo_access_env import ADMIN, GRANTED_REPO
from tests.unit.server.routers.activated_repo_access_env import (  # noqa: F401
    call,
    client,
    env,
    server_db_template,
)
from tests.unit.server.services._git_confirm_helpers import (  # noqa: F401
    make_repo,
    singleton_confirmation_store_fixture,
)

INDEX_FILE = Path(".code-indexer") / "index" / "collection" / "chunks.db"
INDEX_CONFIG = Path(".code-indexer") / "config.json"
OVERRIDE_FILE = Path(".code-indexer-override.yaml")
STRAY = "untracked_probe.txt"


def _seed(repo: Path) -> None:
    """An untracked stray file next to cidx's own untracked working paths."""
    (repo / INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / INDEX_FILE).write_text("index-bytes")
    (repo / INDEX_CONFIG).write_text("{}")
    (repo / OVERRIDE_FILE).write_text("add_extensions: []\n")
    (repo / STRAY).write_text("stray\n")


def _assert_index_kept(repo: Path, removed: Optional[Any]) -> None:
    assert not (repo / STRAY).exists()
    assert (repo / INDEX_FILE).read_text() == "index-bytes"
    assert (repo / INDEX_CONFIG).read_text() == "{}"
    assert (repo / OVERRIDE_FILE).exists()
    if removed is not None:
        assert STRAY in removed
        assert not any(".code-indexer" in str(p) for p in removed), removed


def _admin() -> User:
    return User(
        username="admin-a",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )


def _mcp_clean(alias: str, token: Optional[str]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"repository_alias": alias}
    if token is not None:
        args["confirmation_token"] = token
    response = git_write.git_clean(args, _admin())
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


def test_mcp_git_clean_keeps_repository_index(
    tmp_path: Path, singleton_confirmation_store: Any
) -> None:
    repo = make_repo(tmp_path, "example-repo")
    _seed(repo)

    def _resolve(alias: str, username: str) -> Tuple[Optional[str], Optional[str]]:
        return str(repo), None

    with patch(
        "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
        side_effect=_resolve,
    ):
        first = _mcp_clean("example-repo", None)
        token = first["confirmation_token_required"]["token"]
        result = _mcp_clean("example-repo", token)

    assert result["success"] is True, result
    _assert_index_kept(repo, result["removed_files"])


def test_rest_git_clean_keeps_repository_index(
    client: Any, env: Any, singleton_confirmation_store: Any
) -> None:
    env.activate_for(ADMIN, GRANTED_REPO, "clean-probe")
    repo = Path(
        env.activated_repo_manager.get_activated_repo_path(ADMIN, "clean-probe")
    )
    _seed(repo)
    route = "/api/v1/repos/{a}/git/clean"

    first = call(client, ADMIN, ("POST", route, {"json": {}}, 200), "clean-probe")
    assert first.status_code == 200, first.text
    token = first.json()["token"]
    confirmed = ("POST", route, {"json": {"confirmation_token": token}}, 200)
    second = call(client, ADMIN, confirmed, "clean-probe")

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["success"] is True, body
    _assert_index_kept(repo, body["removed_files"])


def test_pre_refresh_clearing_keeps_repository_index(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "example-repo")
    _seed(repo)

    GitStateManager(config=None).clear_repo_before_refresh(repo_path=repo)

    _assert_index_kept(repo, None)


@pytest.mark.parametrize("nested", [False, True])
def test_clean_keeps_index_directory_at_any_depth(
    tmp_path: Path, singleton_confirmation_store: Any, nested: bool
) -> None:
    """An index directory is kept at the root and in a subdirectory."""
    repo = make_repo(tmp_path, "example-repo")
    index_dir = repo / ("sub" if nested else ".") / ".code-indexer"
    index_dir.mkdir(parents=True)
    (index_dir / "marker").write_text("x")
    binding = {"username": "admin-a", "repo_alias": "example-repo"}
    token = git_operations_service.git_clean(repo, **binding)["token"]

    git_operations_service.git_clean(repo, confirmation_token=token, **binding)

    assert (index_dir / "marker").read_text() == "x"
