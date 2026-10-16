"""Mutating MCP git tools never run inside an immutable versioned snapshot.

Invariant: a global alias resolves (through its alias JSON ``target_path``)
to ``.../.versioned/{alias}/v_<ts>``. Every MUTATING git tool refuses that
path with a client error, before any git subprocess runs, so the snapshot's
working tree, index and refs stay exactly as they were. Mutations on a
normal activated repository still run.

The real handlers, the real ``_resolve_git_repo_path`` and the real alias
JSON resolution run against real git repositories; only the global
registry lookup, the access-filtering service and the activated-repo
lookup are replaced.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Set
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import git_write
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from tests.unit.server.services._git_confirm_helpers import (
    TRACKED,
    UNTRACKED,
    SharedStore,
    git,
    make_repo,
    shared_store_fixture,  # noqa: F401 -- registers the `shared_store` fixture
)
from tests.unit.server.git._running_server_snapshot_manager import (
    wire_running_server_snapshot_manager,
)

_GLOBAL_ALIAS = "example-repo-global"
_ACTIVATED_ALIAS = "example-repo"
_LEGACY = "code_indexer.server.mcp.handlers._legacy"


def _admin() -> User:
    return User(
        username="admin-a",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )


def _parse(response: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


def _state(repo: Path) -> str:
    """Observable git state: refs, index/working-tree status, files."""
    return "\n".join(
        [
            git(["for-each-ref"], repo),
            git(["status", "--porcelain", "--untracked-files=all"], repo),
            "|".join(sorted(p.name for p in repo.iterdir())),
        ]
    )


class _ActivatedRepos:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, data_dir: str) -> "_ActivatedRepos":
        return self

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        assert user_alias == _ACTIVATED_ALIAS
        return str(self._path)


@pytest.fixture
def env(tmp_path: Path, shared_store: SharedStore) -> Iterator[Dict[str, Path]]:
    golden = tmp_path / "golden-repos"
    snapshot = make_repo(golden / ".versioned" / "example-repo", "v_1700000000")
    aliases = golden / "aliases"
    aliases.mkdir()
    AliasManager(str(aliases)).create_alias(_GLOBAL_ALIAS, str(snapshot))
    activated = make_repo(tmp_path / "activated" / "admin-a", _ACTIVATED_ALIAS)

    with (
        patch.object(git_operations_service, "payload_cache", shared_store.new_cache()),
        patch(f"{_LEGACY}._get_golden_repos_dir", return_value=str(golden)),
        patch(
            f"{_LEGACY}._get_global_repo",
            MagicMock(return_value={"repo_url": "https://example.com/r.git"}),
        ),
        patch(f"{_LEGACY}._get_access_filtering_service", return_value=None),
        patch(f"{_LEGACY}.ActivatedRepoManager", _ActivatedRepos(activated)),
    ):
        yield {"snapshot": snapshot, "activated": activated}


def _clean(alias: str) -> Dict[str, Any]:
    """git_clean, presenting the issued confirmation token if one comes back."""
    first = _parse(git_write.git_clean({"repository_alias": alias}, _admin()))
    required = first.get("confirmation_token_required")
    if not isinstance(required, dict):
        return first
    return _parse(
        git_write.git_clean(
            {"repository_alias": alias, "confirmation_token": required["token"]},
            _admin(),
        )
    )


def _reset(alias: str) -> Dict[str, Any]:
    first = _parse(
        git_write.git_reset({"repository_alias": alias, "mode": "hard"}, _admin())
    )
    required = first.get("confirmation_token_required")
    if not isinstance(required, dict):
        return first
    return _parse(
        git_write.git_reset(
            {
                "repository_alias": alias,
                "mode": "hard",
                "confirmation_token": required["token"],
            },
            _admin(),
        )
    )


_MUTATIONS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "git_clean": _clean,
    "git_reset": _reset,
    "git_stage": lambda alias: _parse(
        git_write.git_stage(
            {"repository_alias": alias, "file_paths": [UNTRACKED]}, _admin()
        )
    ),
    "git_branch_create": lambda alias: _parse(
        git_write.git_branch_create(
            {"repository_alias": alias, "branch_name": "new-branch"}, _admin()
        )
    ),
    "git_checkout_file": lambda alias: _parse(
        git_write.git_checkout_file(
            {"repository_alias": alias, "file_path": TRACKED}, _admin()
        )
    ),
    "git_stash": lambda alias: _parse(
        git_write.git_stash({"repository_alias": alias, "action": "push"}, _admin())
    ),
}


@pytest.mark.parametrize("tool", sorted(_MUTATIONS))
def test_mutating_tool_on_versioned_snapshot_is_refused_and_changes_nothing(
    env: Dict[str, Path], tool: str
) -> None:
    snapshot = env["snapshot"]
    before = _state(snapshot)

    result = _MUTATIONS[tool](_GLOBAL_ALIAS)

    assert result.get("success") is False, result
    assert "confirmation_token_required" not in result, result
    assert "snapshot" in result["error"].lower(), result
    assert _state(snapshot) == before


@pytest.fixture
def ontap_env(
    tmp_path: Path,
    shared_store: SharedStore,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """A global alias whose ``target_path`` is a flat ONTAP-shaped snapshot
    ``{mount}/v_<ts>``, inside a running server whose wired snapshot manager
    has its clone backend mounted at ``{mount}``."""
    mount = tmp_path / "ontap-mount"
    snapshot = make_repo(mount, "v_1700000000")
    golden = tmp_path / "golden-repos"
    aliases = golden / "aliases"
    aliases.mkdir(parents=True)
    AliasManager(str(aliases)).create_alias(_GLOBAL_ALIAS, str(snapshot))
    wire_running_server_snapshot_manager(monkeypatch, str(mount))

    with (
        patch.object(git_operations_service, "payload_cache", shared_store.new_cache()),
        patch(f"{_LEGACY}._get_golden_repos_dir", return_value=str(golden)),
        patch(
            f"{_LEGACY}._get_global_repo",
            MagicMock(return_value={"repo_url": "https://example.com/r.git"}),
        ),
        patch(f"{_LEGACY}._get_access_filtering_service", return_value=None),
    ):
        yield snapshot


@pytest.mark.parametrize("tool", ["git_clean", "git_reset"])
def test_mutating_tool_on_flat_ontap_snapshot_is_refused_and_changes_nothing(
    ontap_env: Path, tool: str
) -> None:
    before = _state(ontap_env)

    result = _MUTATIONS[tool](_GLOBAL_ALIAS)

    assert result.get("success") is False, result
    assert "confirmation_token_required" not in result, result
    assert "snapshot" in result["error"].lower(), result
    assert _state(ontap_env) == before


def _functions_calling(source: str, callee: str) -> Set[str]:
    """Names of the top-level functions in *source* that call *callee*
    (as a bare name or as an attribute, e.g. ``_legacy.<callee>``)."""
    callers: Set[str] = set()
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            if isinstance(func, ast.Name):
                name = func.id
            if name == callee:
                callers.add(node.name)
    return callers


def test_only_the_mutable_resolver_calls_resolve_git_repo_path() -> None:
    """Every git write tool reaches its repository through the snapshot
    guard: no handler resolves the path on its own."""
    source = Path(git_write.__file__).read_text(encoding="utf-8")

    assert _functions_calling(source, "_resolve_git_repo_path") == {
        "_resolve_mutable_git_repo_path"
    }


def test_git_clean_on_activated_repo_still_removes_untracked_files(
    env: Dict[str, Path],
) -> None:
    activated = env["activated"]
    assert (activated / UNTRACKED).exists()

    result = _clean(_ACTIVATED_ALIAS)

    assert result.get("success") is True, result
    assert not (activated / UNTRACKED).exists()
