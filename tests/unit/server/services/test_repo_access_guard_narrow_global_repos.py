"""narrow_global_repos_to_accessible: query searches only accessible repos.

Real AccessFilteringService over a real GroupAccessManager (temp SQLite);
nothing about the access decision is mocked. Aliases and usernames are
neutral placeholders.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    narrow_global_repos_to_accessible,
)

USER = "example_user"
ADMIN = "example_admin"
NO_GROUP_USER = "example_nobody"


@pytest.fixture
def service(tmp_path: Path) -> AccessFilteringService:
    gam = GroupAccessManager(tmp_path / "groups.db")
    group = gam.create_group("restricted", "test group")
    gam.assign_user_to_group(USER, group.id, assigned_by="test")
    gam.grant_repo_access("example-repo", group.id, granted_by="test")
    admins = gam.get_group_by_name("admins")
    assert admins is not None
    gam.assign_user_to_group(ADMIN, admins.id, assigned_by="test")
    return AccessFilteringService(gam)


def _global(alias: str) -> Dict[str, Any]:
    return {"user_alias": alias, "username": "global", "is_global": True}


def _repos() -> List[Dict[str, Any]]:
    return [
        # The caller's own activated repo: never narrowed here, even though
        # its user_alias is granted to no group.
        {"user_alias": "my-activated-repo", "golden_repo_alias": "other-repo"},
        _global("example-repo-global"),
        _global("other-repo-global"),
        _global("cidx-meta-global"),
    ]


def _aliases(repos: List[Dict[str, Any]]) -> List[str]:
    return [r["user_alias"] for r in repos]


def test_non_admin_keeps_activated_and_granted_global_repos(service):
    narrowed = narrow_global_repos_to_accessible(service, USER, _repos())

    assert _aliases(narrowed) == [
        "my-activated-repo",
        "example-repo-global",
        "cidx-meta-global",
    ]


def test_user_without_group_keeps_only_activated_and_cidx_meta(service):
    narrowed = narrow_global_repos_to_accessible(service, NO_GROUP_USER, _repos())

    assert _aliases(narrowed) == ["my-activated-repo", "cidx-meta-global"]


def test_admin_keeps_every_repo_including_ungrouped_ones(service):
    repos = _repos()

    assert narrow_global_repos_to_accessible(service, ADMIN, repos) == repos


@pytest.mark.parametrize("username", [USER, ADMIN])
def test_missing_service_with_global_repos_in_scope_fails_closed(username, caplog):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(AccessFilteringServiceUnavailableError):
            narrow_global_repos_to_accessible(None, username, _repos())

    assert any(
        r.levelno == logging.ERROR and "QUERY-MIGRATE-014" in r.getMessage()
        for r in caplog.records
    )


def test_missing_service_with_explicit_global_alias_fails_closed():
    with pytest.raises(AccessFilteringServiceUnavailableError):
        narrow_global_repos_to_accessible(
            None, USER, _repos(), repository_alias="example-repo-global"
        )


def test_missing_service_scoped_to_own_activation_needs_no_decision():
    narrowed = narrow_global_repos_to_accessible(
        None, USER, _repos(), repository_alias="my-activated-repo"
    )

    assert _aliases(narrowed) == ["my-activated-repo"]


def test_ungranted_alias_does_not_widen_narrowing_for_non_admin(service):
    narrowed = narrow_global_repos_to_accessible(
        service, USER, _repos(), repository_alias="other-repo-global"
    )

    assert _aliases(narrowed) == [
        "my-activated-repo",
        "example-repo-global",
        "cidx-meta-global",
    ]


def test_admin_alias_of_ungrouped_repo_is_kept(service):
    narrowed = narrow_global_repos_to_accessible(
        service, ADMIN, _repos(), repository_alias="other-repo-global"
    )

    assert "other-repo-global" in _aliases(narrowed)
