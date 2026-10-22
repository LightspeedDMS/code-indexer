"""History of the internal catalog is admin-only on the MCP branch tools.

Branch listings carry commit metadata and branch switching moves a clone
through history, so get_branches and switch_branch follow the same rule as
the git tools: judged by the golden identity of the repository served,
refused for restricted callers, kept for admins. Real git repositories, a
real alias registry and a real ActivatedRepoManager (the fixture of
test_meta_history_surfaces_denied). Neutral names only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
)

from . import test_meta_history_surfaces_denied as _history
from .test_meta_history_surfaces_denied import (
    CATALOG_CLONE,
    _activate,
    _call,
    _user,
)

# The shared real-repository fixtures, re-exported for pytest.
module_tmp = _history.module_tmp
fx = _history.fx
server = _history.server

GIT_REFUSAL = CatalogSurface.GIT_HISTORY.refusal
COMPOSITE = "mixed-notes"


def _rewrite_metadata(
    server: SimpleNamespace, username: str, alias: str, **fields: Any
) -> None:
    arm = server.state.activated_repo_manager
    path = Path(arm.activated_repos_dir) / username / f"{alias}_metadata.json"
    record = json.loads(path.read_text())
    record.update(fields)
    path.write_text(json.dumps(record))


@pytest.fixture(scope="module")
def composite(server: SimpleNamespace) -> str:
    data_dir = Path(server.state.activated_repo_manager.activated_repos_dir).parent
    _activate(data_dir, server.catalog, RESTRICTED_USER, COMPOSITE)
    _rewrite_metadata(
        server,
        RESTRICTED_USER,
        COMPOSITE,
        is_composite=True,
        golden_repo_aliases=[ALPHA, "cidx-meta"],
    )
    return COMPOSITE


def _branches(
    alias: str, username: str, role: UserRole = UserRole.ADMIN
) -> Dict[str, Any]:
    # Role ADMIN passes role gates; visibility is decided by admins-group
    # membership, so RESTRICTED_USER stays restricted with this role.
    return _call("get_branches", {"repository_alias": alias}, _user(username, role))


@pytest.mark.parametrize("alias", ["cidx-meta-global", "cidx-meta", CATALOG_CLONE])
def test_get_branches_refuses_catalog_history_for_restricted_caller(
    server: SimpleNamespace, alias: str
) -> None:
    data = _branches(alias, RESTRICTED_USER)
    assert data.get("success") is False, data
    assert data.get("error") == GIT_REFUSAL, data
    assert "last_commit" not in json.dumps(data)


def test_get_branches_refuses_a_composite_containing_the_catalog(
    server: SimpleNamespace, composite: str
) -> None:
    data = _branches(composite, RESTRICTED_USER, UserRole.NORMAL_USER)
    assert data.get("error") == GIT_REFUSAL, data


def test_admin_lists_catalog_clone_branches(server: SimpleNamespace) -> None:
    data = _branches(CATALOG_CLONE, ADMIN_USER)
    assert data.get("success") is True, data
    assert data["branches"][0]["last_commit"]["message"]


def _clone_git(server: SimpleNamespace, username: str, *args: str) -> str:
    arm = server.state.activated_repo_manager
    clone = Path(arm.activated_repos_dir) / username / CATALOG_CLONE
    done = subprocess.run(
        ["git", *args], cwd=clone, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _ensure_older_branch(server: SimpleNamespace, username: str) -> None:
    if not _clone_git(server, username, "branch", "--list", "older"):
        _clone_git(server, username, "branch", "older", "HEAD~1")


@pytest.mark.parametrize(
    "branch, create",
    [("older", False), ("fresh-start", True)],
    ids=["switch", "create"],
)
def test_switch_branch_refuses_a_catalog_clone(
    server: SimpleNamespace, branch: str, create: bool
) -> None:
    _ensure_older_branch(server, RESTRICTED_USER)
    before = _clone_git(server, RESTRICTED_USER, "branch", "--list")
    args = {"user_alias": CATALOG_CLONE, "branch_name": branch, "create": create}
    data = _call("switch_branch", args, _user(RESTRICTED_USER, UserRole.NORMAL_USER))
    assert data.get("success") is False, data
    assert data.get("error") == GIT_REFUSAL, data
    assert _clone_git(server, RESTRICTED_USER, "branch", "--show-current") == "main"
    assert _clone_git(server, RESTRICTED_USER, "branch", "--list") == before


def test_admin_switches_own_catalog_clone(server: SimpleNamespace) -> None:
    _ensure_older_branch(server, ADMIN_USER)
    args = {"user_alias": CATALOG_CLONE, "branch_name": "older"}
    data = _call("switch_branch", args, _user(ADMIN_USER, UserRole.ADMIN))
    try:
        assert data.get("success") is True, data
        assert _clone_git(server, ADMIN_USER, "branch", "--show-current") == "older"
    finally:
        _clone_git(server, ADMIN_USER, "checkout", "-q", "main")
