"""MCP git write tools never run git inside a shared golden repository.

Invariant: a ``-global`` alias names the golden repository every user's
queries read. Its alias JSON ``target_path`` can be the golden repository's
own clone (not only a ``.versioned`` snapshot). Every git WRITE tool refuses
a ``-global`` alias with the write-mode error, before any git process starts
in that repository, unless write mode is active for it (the same gate
``git_merge`` and ``git_mark_resolved`` already apply). Activated
repositories keep working; read tools on ``-global`` are unaffected.

"No git process started" is observed for real: a ``git`` shim placed first
on PATH records the working directory and argv of every git process, then
runs the real git. The real handlers, the real resolver and the real alias
JSON resolution run against real repositories; only the global registry
lookup, the access-filtering service and the activated-repo lookup are
replaced.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import git_read, git_write
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from tests.unit.server.services._git_confirm_helpers import (
    BRANCH,
    TRACKED,
    UNTRACKED,
    SharedStore,
    git,
    make_repo,
    shared_store_fixture,  # noqa: F401 -- registers the `shared_store` fixture
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
    """Observable git state: refs, stash, index/working-tree status, files."""
    return "\n".join(
        [
            git(["for-each-ref"], repo),
            git(["stash", "list"], repo),
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

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Dict[str, Any]:
        return {"user_alias": user_alias, "golden_repo_alias": "example-repo"}


class _GitLog:
    """Reads the shim's record of every git process started."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def clear(self) -> None:
        """Forget processes started by test setup (the helpers run git too)."""
        self._path.unlink(missing_ok=True)

    def runs_in(self, repo: Path) -> list:
        if not self._path.exists():
            return []
        real = str(repo.resolve())
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if real in line]


def _install_git_shim(bin_dir: Path, log: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s\\n" "$(pwd -P)" "$*" >> "{log}"\n'
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def env(
    tmp_path: Path, shared_store: SharedStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Dict[str, Any]]:
    golden = tmp_path / "golden-repos"
    shared = make_repo(golden, "example-repo")
    aliases = golden / "aliases"
    aliases.mkdir()
    AliasManager(str(aliases)).create_alias(_GLOBAL_ALIAS, str(shared))
    activated = make_repo(tmp_path / "activated-repos" / "admin-a", _ACTIVATED_ALIAS)

    log = tmp_path / "git-processes.log"
    bin_dir = tmp_path / "bin"
    _install_git_shim(bin_dir, log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    # A running server's app.state, without constructing the real app.
    running_app = SimpleNamespace(
        state=SimpleNamespace(
            golden_repos_dir=str(golden),
            snapshot_manager=None,
            activated_repo_manager=_ActivatedRepos(activated),
        )
    )
    server_app = sys.modules["code_indexer.server.app"]
    monkeypatch.setitem(vars(server_app), "app", running_app)
    # The write-mode gate reads git_write's own app_module binding; point it
    # at the module patched above so the gate always sees this running app.
    monkeypatch.setattr(git_write, "app_module", server_app)

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
        yield {
            "golden": golden,
            "shared": shared,
            "activated": activated,
            "git_log": _GitLog(log),
        }


def _with_token(tool: Callable[..., Dict[str, Any]], args: Dict[str, Any]) -> Any:
    """Call *tool*, presenting the issued confirmation token if one comes back."""
    first = _parse(tool(dict(args), _admin()))
    required = first.get("confirmation_token_required")
    if not isinstance(required, dict):
        return first
    return _parse(tool({**args, "confirmation_token": required["token"]}, _admin()))


def _call(tool: Callable[..., Dict[str, Any]], **extra: Any) -> Callable[[str], Any]:
    return lambda alias: _parse(tool({"repository_alias": alias, **extra}, _admin()))


_WRITE_TOOLS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "git_stage": _call(git_write.git_stage, file_paths=[UNTRACKED]),
    "git_unstage": _call(git_write.git_unstage, file_paths=[TRACKED]),
    "git_commit": _call(git_write.git_commit, message="change"),
    "git_amend": _call(git_write.git_amend, message="rewritten"),
    "git_stash": _call(git_write.git_stash, action="push"),
    "git_branch_create": _call(git_write.git_branch_create, branch_name="new-branch"),
    "git_checkout_file": _call(git_write.git_checkout_file, file_path=TRACKED),
    "git_merge": _call(git_write.git_merge, source_branch=BRANCH),
    "git_merge_abort": _call(git_write.git_merge_abort),
    "git_mark_resolved": _call(git_write.git_mark_resolved, file_path=TRACKED),
    "git_push": _call(git_write.git_push),
    "git_reset": lambda alias: _with_token(
        git_write.git_reset, {"repository_alias": alias, "mode": "hard"}
    ),
    "git_clean": lambda alias: _with_token(
        git_write.git_clean, {"repository_alias": alias}
    ),
    "git_branch_delete": lambda alias: _with_token(
        git_write.git_branch_delete,
        {"repository_alias": alias, "branch_name": BRANCH},
    ),
}


@pytest.mark.parametrize("tool", sorted(_WRITE_TOOLS))
def test_write_tool_on_global_alias_is_refused_before_any_git_process(
    env: Dict[str, Any], tool: str
) -> None:
    shared: Path = env["shared"]
    before = _state(shared)
    env["git_log"].clear()

    result = _WRITE_TOOLS[tool](_GLOBAL_ALIAS)

    assert result.get("success") is False, result
    assert "confirmation_token_required" not in result, result
    error = result["error"].lower()
    assert "write mode required" in error, result
    assert "activate the repository" in error, result
    assert env["git_log"].runs_in(shared) == []
    assert _state(shared) == before


def test_branch_switch_on_global_alias_keeps_its_refusal(env: Dict[str, Any]) -> None:
    env["git_log"].clear()

    result = _parse(
        git_write.git_branch_switch(
            {"repository_alias": _GLOBAL_ALIAS, "branch_name": BRANCH}, _admin()
        )
    )

    assert result.get("success") is False, result
    assert "golden repositories" in result["error"], result
    assert env["git_log"].runs_in(env["shared"]) == []


def _enter_write_mode(golden: Path) -> None:
    marker_dir = golden / ".write_mode"
    marker_dir.mkdir()
    (marker_dir / f"{_ACTIVATED_ALIAS}.json").write_text("{}", encoding="utf-8")


def test_write_mode_lets_branch_create_run_on_global_alias(
    env: Dict[str, Any],
) -> None:
    _enter_write_mode(env["golden"])

    result = _WRITE_TOOLS["git_branch_create"](_GLOBAL_ALIAS)

    assert result.get("success") is True, result
    assert "new-branch" in git(["branch", "--list"], env["shared"])


def test_write_mode_lets_merge_run_on_global_alias(env: Dict[str, Any]) -> None:
    _enter_write_mode(env["golden"])
    git(["stash", "-u"], env["shared"])  # merge needs a clean working tree
    env["git_log"].clear()

    result = _WRITE_TOOLS["git_merge"](_GLOBAL_ALIAS)

    assert result.get("success") is True, result
    assert env["git_log"].runs_in(env["shared"]) != []


def test_write_mode_lets_mark_resolved_reach_git_on_global_alias(
    env: Dict[str, Any],
) -> None:
    _enter_write_mode(env["golden"])

    result = _WRITE_TOOLS["git_mark_resolved"](_GLOBAL_ALIAS)

    assert "write mode required" not in str(result.get("error", "")).lower()
    assert env["git_log"].runs_in(env["shared"]) != []


def test_activated_repo_stage_and_commit_still_work(env: Dict[str, Any]) -> None:
    activated: Path = env["activated"]

    staged = _parse(
        git_write.git_stage(
            {"repository_alias": _ACTIVATED_ALIAS, "file_paths": [TRACKED]}, _admin()
        )
    )
    committed = _WRITE_TOOLS["git_commit"](_ACTIVATED_ALIAS)

    assert staged.get("success") is True, staged
    assert committed.get("success") is True, committed
    assert git(["log", "-1", "--format=%s"], activated).strip() == "change"


def test_activated_repo_branch_create_still_works(env: Dict[str, Any]) -> None:
    result = _WRITE_TOOLS["git_branch_create"](_ACTIVATED_ALIAS)

    assert result.get("success") is True, result
    assert "new-branch" in git(["branch", "--list"], env["activated"])


def test_activated_repo_reset_still_works(env: Dict[str, Any]) -> None:
    activated: Path = env["activated"]
    git(["add", TRACKED], activated)

    result = _parse(
        git_write.git_reset(
            {"repository_alias": _ACTIVATED_ALIAS, "mode": "mixed"}, _admin()
        )
    )

    assert result.get("success") is True, result
    assert git(["diff", "--cached", "--name-only"], activated).strip() == ""


def test_activated_repo_merge_still_works(env: Dict[str, Any]) -> None:
    git(["stash", "-u"], env["activated"])

    result = _WRITE_TOOLS["git_merge"](_ACTIVATED_ALIAS)

    assert result.get("success") is True, result


def test_read_tool_on_global_alias_still_works(env: Dict[str, Any]) -> None:
    env["git_log"].clear()

    result = _parse(git_read.git_status({"repository_alias": _GLOBAL_ALIAS}, _admin()))

    assert result.get("success") is True, result
    assert env["git_log"].runs_in(env["shared"]) != []
