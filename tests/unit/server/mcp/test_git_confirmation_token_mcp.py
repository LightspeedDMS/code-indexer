"""MCP git_reset (hard), git_clean and git_branch_delete: confirmation
tokens through the real handlers, on SQLite and PostgreSQL.

Invariants:
  - an invalid token returns ``success: false`` plus
    ``confirmation_token_required`` with a fresh token (never an internal
    error), and that fresh token confirms the operation;
  - the handlers bind tokens to the calling user and the repository alias.

The real GitOperationsService singleton runs against real git repositories
and a real PayloadCache; only alias-to-path resolution is patched.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import git_write
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from tests.unit.server.services._git_confirm_helpers import (
    BRANCH,
    COMMITTED_TEXT,
    REPO_A,
    REPO_B,
    TRACKED,
    UNTRACKED,
    SharedStore,
    git,
    make_repo,
    shared_store_fixture,  # noqa: F401 -- registers the `shared_store` fixture
)

_BOGUS_TOKEN = "ZZZZZZ"


def _admin(username: str) -> User:
    return User(
        username=username,
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )


def _parse(response: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


def _call_clean(alias: str, user: User, token: Optional[str]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"repository_alias": alias}
    if token is not None:
        args["confirmation_token"] = token
    return _parse(git_write.git_clean(args, user))


def _call_reset(alias: str, user: User, token: Optional[str]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"repository_alias": alias, "mode": "hard"}
    if token is not None:
        args["confirmation_token"] = token
    return _parse(git_write.git_reset(args, user))


def _call_delete(alias: str, user: User, token: Optional[str]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"repository_alias": alias, "branch_name": BRANCH}
    if token is not None:
        args["confirmation_token"] = token
    return _parse(git_write.git_branch_delete(args, user))


_TOOLS: Dict[str, Tuple[Callable[..., Dict[str, Any]], Callable[[Path], bool]]] = {
    "git_clean": (_call_clean, lambda r: not (r / UNTRACKED).exists()),
    "git_reset": (_call_reset, lambda r: (r / TRACKED).read_text() == COMMITTED_TEXT),
    "git_branch_delete": (
        _call_delete,
        lambda r: git(["branch", "--list", BRANCH], r).strip() == "",
    ),
}


@pytest.fixture
def repos(tmp_path: Path, shared_store: SharedStore) -> Iterator[Dict[str, Path]]:
    paths = {
        REPO_A: make_repo(tmp_path / "repos", REPO_A),
        REPO_B: make_repo(tmp_path / "repos", REPO_B),
    }

    def _resolve(alias: str, username: str) -> Tuple[Optional[str], Optional[str]]:
        if alias not in paths:
            return None, f"Repository '{alias}' not found"
        return str(paths[alias]), None

    with (
        patch.object(git_operations_service, "payload_cache", shared_store.new_cache()),
        patch(
            "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
            side_effect=_resolve,
        ),
    ):
        yield paths


def _token_of(data: Dict[str, Any]) -> str:
    assert data.get("success") is False, data
    required = data.get("confirmation_token_required")
    assert isinstance(required, dict), data
    token = required.get("token")
    assert isinstance(token, str) and token, data
    return token


@pytest.mark.parametrize("tool", sorted(_TOOLS))
def test_invalid_token_returns_fresh_token_that_confirms(
    repos: Dict[str, Path], tool: str
) -> None:
    call, happened = _TOOLS[tool]
    admin = _admin("admin-a")

    rejected = call(REPO_A, admin, _BOGUS_TOKEN)

    fresh = _token_of(rejected)
    assert fresh != _BOGUS_TOKEN
    assert "invalid" in rejected["confirmation_token_required"]["message"].lower()
    assert not happened(repos[REPO_A])

    confirmed = call(REPO_A, admin, fresh)

    assert confirmed.get("success") is True, confirmed
    assert happened(repos[REPO_A])


@pytest.mark.parametrize("tool", sorted(_TOOLS))
def test_confirmation_message_never_contains_the_token(
    repos: Dict[str, Path], tool: str
) -> None:
    call, _ = _TOOLS[tool]
    admin = _admin("admin-a")

    for presented in (None, _BOGUS_TOKEN):
        data = call(REPO_A, admin, presented)
        token = _token_of(data)
        message = data["confirmation_token_required"]["message"]

        assert token not in message
        assert "confirmation_token" in message
        assert "`token`" in message


def test_token_issued_to_one_user_is_rejected_for_another(
    repos: Dict[str, Path],
) -> None:
    token = _token_of(_call_clean(REPO_A, _admin("admin-a"), None))

    result = _call_clean(REPO_A, _admin("admin-b"), token)

    assert _token_of(result) != token
    assert (repos[REPO_A] / UNTRACKED).exists()


def test_token_issued_for_one_alias_is_rejected_for_another(
    repos: Dict[str, Path],
) -> None:
    admin = _admin("admin-a")
    token = _token_of(_call_clean(REPO_A, admin, None))

    result = _call_clean(REPO_B, admin, token)

    assert _token_of(result) != token
    assert (repos[REPO_B] / UNTRACKED).exists()
