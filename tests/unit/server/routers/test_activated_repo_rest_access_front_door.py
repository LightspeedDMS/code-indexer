# ruff: noqa: F811
# (the shared fixtures imported below are pytest fixtures used as test
# parameters, which ruff reports as redefinitions)
"""REST routes that serve a caller's ACTIVATED repository judge it by the
golden repositories that activation was created from -- the rule the MCP
dispatcher's repository pre-check applies (AccessFilteringService.
alias_granted over caller_activation_sources).

Driven through the real REST front door of a real app (``create_app`` via
``isolated_app``, never ~/.cidx-server). The services the routes reach are
the real ones (QueryAccessEnv): AccessFilteringService over a real
GroupAccessManager, real ActivatedRepoManager activations (real git
clones), GoldenRepoManager and the global registry. Nothing about the
access decision is mocked.

A refused activation must answer exactly what the same route answers for
an alias the caller never activated, so a refusal never reveals that the
activation exists.

Repository aliases and usernames are neutral placeholders.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import pytest

from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    GRANTED_REPO,
    GRANTED_REPO_2,
    OWN_ACTIVATION,
    UNGRANTED_REPO,
    USER,
)
from tests.unit.server.routers.activated_repo_access_env import (  # noqa: F401
    COMPOSITE_ALIAS,
    PEER,
    PEER_ACTIVATION,
    README,
    README_TEXT,
    SWEEPER,
    UNKNOWN_ALIAS,
    Probe,
    activate_composite,
    assert_refused_as_unknown,
    call,
    client,
    env,
    server_db_template,
    shape,
)

_GUARD_LOGGER = "code_indexer.server.routers.repo_access_http"

# Routes the principal tests drive (each answers 200 to an allowed caller).
PROBES: Dict[str, Probe] = {
    "git_cat": ("GET", "/api/v1/repos/{a}/git/cat", {"params": {"path": README}}, 200),
    "git_blame": (
        "GET",
        "/api/v1/repos/{a}/git/blame",
        {"params": {"path": README}},
        200,
    ),
    "git_file_history": (
        "GET",
        "/api/v1/repos/{a}/git/file-history",
        {"params": {"path": README}},
        200,
    ),
    # A git_operations_service route.
    "git_status": ("GET", "/api/v1/repos/{a}/git/status", {}, 200),
    # A write route.
    "git_stage": (
        "POST",
        "/api/v1/repos/{a}/git/stage",
        {"json": {"file_paths": [README]}},
        200,
    ),
    "repo_files": ("GET", "/api/repos/{a}/files", {}, 200),
    # An async route (activated_repos router).
    "activated_branches": ("GET", "/api/activated-repos/{a}/branches", {}, 200),
    "v2_details": ("GET", "/api/repositories/{a}", {}, 200),
}

# Every REST route that resolves the caller's activated repository by the
# alias it is given (allowed status unused: the sweep compares refusals).
SWEEP: Dict[str, Probe] = {
    **PROBES,
    "git_diff": ("GET", "/api/v1/repos/{a}/git/diff", {}, 0),
    "git_log": ("GET", "/api/v1/repos/{a}/git/log", {}, 0),
    "git_unstage": (
        "POST",
        "/api/v1/repos/{a}/git/unstage",
        {"json": {"file_paths": [README]}},
        0,
    ),
    "git_commit": (
        "POST",
        "/api/v1/repos/{a}/git/commit",
        {"json": {"message": "m"}},
        0,
    ),
    "git_push": ("POST", "/api/v1/repos/{a}/git/push", {"json": {}}, 0),
    "git_pull": ("POST", "/api/v1/repos/{a}/git/pull", {"json": {}}, 0),
    "git_fetch": ("POST", "/api/v1/repos/{a}/git/fetch", {"json": {}}, 0),
    "git_reset": ("POST", "/api/v1/repos/{a}/git/reset", {"json": {"mode": "soft"}}, 0),
    "git_clean": ("POST", "/api/v1/repos/{a}/git/clean", {"json": {}}, 0),
    "git_merge_abort": ("POST", "/api/v1/repos/{a}/git/merge-abort", {}, 0),
    "git_checkout_file": (
        "POST",
        "/api/v1/repos/{a}/git/checkout-file",
        {"json": {"file_path": README}},
        0,
    ),
    "git_branch_list": ("GET", "/api/v1/repos/{a}/git/branches", {}, 0),
    "git_branch_create": (
        "POST",
        "/api/v1/repos/{a}/git/branches",
        {"json": {"branch_name": "feature"}},
        0,
    ),
    "git_branch_switch": ("POST", "/api/v1/repos/{a}/git/branches/main/switch", {}, 0),
    "git_branch_delete": ("DELETE", "/api/v1/repos/{a}/git/branches/feature", {}, 0),
    "file_create": (
        "POST",
        "/api/v1/repos/{a}/files",
        {"json": {"file_path": "new.txt", "content": "x"}},
        0,
    ),
    "file_edit": (
        "PATCH",
        f"/api/v1/repos/{{a}}/files/{README}",
        {"json": {"old_string": "e", "new_string": "f", "content_hash": "0"}},
        0,
    ),
    "file_delete": ("DELETE", f"/api/v1/repos/{{a}}/files/{README}", {}, 0),
    "reindex": (
        "POST",
        "/api/v1/repos/{a}/reindex",
        {"json": {"index_types": ["semantic"]}},
        0,
    ),
    "index_status": ("GET", "/api/v1/repos/{a}/index-status", {}, 0),
    "temporal_status": ("GET", "/api/v1/repos/{a}/temporal-status", {}, 0),
    "activated_indexes": ("GET", "/api/activated-repos/{a}/indexes", {}, 0),
    "activated_reindex": ("POST", "/api/activated-repos/{a}/reindex", {"json": {}}, 0),
    "activated_add_index": (
        "POST",
        "/api/activated-repos/{a}/indexes/semantic",
        {},
        0,
    ),
    "activated_health": ("POST", "/api/activated-repos/{a}/health/check", {}, 0),
    "activated_sync": ("POST", "/api/activated-repos/{a}/sync", {"json": {}}, 0),
    "activated_branch": (
        "POST",
        "/api/activated-repos/{a}/branch",
        {"json": {"branch_name": "main"}},
        0,
    ),
    "repo_branch_switch": (
        "PUT",
        "/api/repos/{a}/branch",
        {"json": {"branch_name": "main"}},
        0,
    ),
    "repo_sync": ("PUT", "/api/repos/{a}/sync", {}, 0),
    "repo_sync_general": (
        "POST",
        "/api/repos/sync",
        {"json": {"repository_alias": "{a}"}},
        0,
    ),
    "repo_branches": ("GET", "/api/repos/{a}/branches", {}, 0),
    "repo_info": ("GET", "/api/repos/{a}", {}, 0),
    "repo_sync_status": ("GET", "/api/repos/{a}/sync-status", {}, 0),
    "v2_branches": ("GET", "/api/repositories/{a}/branches", {}, 0),
    "v2_sync": ("POST", "/api/repositories/{a}/sync", {}, 0),
    "v2_stats": ("GET", "/api/repositories/{a}/stats", {}, 0),
    "v2_files": ("GET", "/api/repositories/{a}/files", {}, 0),
    "v2_file_content": (
        "GET",
        "/api/repositories/{a}/files",
        {"params": {"content": "true", "path": README}},
        0,
    ),
}


_PRINCIPAL = sorted(PROBES)


class TestOwnGrantedActivation:
    @pytest.mark.parametrize("route", _PRINCIPAL)
    def test_own_activation_of_granted_repo_is_served(self, client, env, route):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        response = call(client, USER, PROBES[route], OWN_ACTIVATION)

        assert response.status_code == PROBES[route][3], response.text

    def test_git_cat_returns_the_file(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        response = call(client, USER, PROBES["git_cat"], OWN_ACTIVATION)

        assert response.json()["content"].strip() == README_TEXT

    @pytest.mark.parametrize("route", sorted(SWEEP))
    def test_granted_activation_is_not_answered_as_unknown(self, client, env, route):
        """The guard never over-refuses: a granted non-admin (every route
        permission, outside the admins group) reaches each route's own
        handling, never the unknown-alias answer."""
        env.activate_for(SWEEPER, GRANTED_REPO, OWN_ACTIVATION)

        granted = call(client, SWEEPER, SWEEP[route], OWN_ACTIVATION)
        unknown = call(client, SWEEPER, SWEEP[route], UNKNOWN_ALIAS)

        assert shape(granted, OWN_ACTIVATION) != shape(unknown, UNKNOWN_ALIAS)


# Routes that also resolve the caller's activation by its GOLDEN alias.
GOLDEN_ALIAS_ROUTES = ("repo_sync_general", "v2_branches", "v2_sync")


class TestGoldenAliasForms:
    @pytest.mark.parametrize("route", GOLDEN_ALIAS_ROUTES)
    def test_revoked_activation_named_by_golden_alias_is_refused(
        self, client, env, route
    ):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)

        assert_refused_as_unknown(client, USER, SWEEP[route], GRANTED_REPO)

    @pytest.mark.parametrize("route", GOLDEN_ALIAS_ROUTES)
    def test_activation_of_ungranted_repo_named_by_golden_alias_is_refused(
        self, client, env, route
    ):
        env.activate_for(USER, UNGRANTED_REPO, OWN_ACTIVATION)

        assert_refused_as_unknown(client, USER, SWEEP[route], UNGRANTED_REPO)

    @pytest.mark.parametrize("route", GOLDEN_ALIAS_ROUTES)
    def test_granted_activation_named_by_golden_alias_is_allowed(
        self, client, env, route
    ):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        granted = call(client, USER, SWEEP[route], GRANTED_REPO)
        unknown = call(client, USER, SWEEP[route], UNKNOWN_ALIAS)

        # The route's own handling (e.g. 409 for a sync already queued by an
        # earlier test in this module's app), never the access refusal.
        assert shape(granted, GRANTED_REPO) != shape(unknown, UNKNOWN_ALIAS)
        assert "access_denied" not in granted.text, granted.text


_INDEXES = SWEEP["activated_indexes"]


def _indexes_owner(client: Any, caller: str, owner: str) -> str:
    """The repo_path GET .../indexes returns to *caller* passing owner=*owner*."""
    response = call(client, caller, _INDEXES, OWN_ACTIVATION, params={"owner": owner})
    assert response.status_code == 200, response.text
    repo_path: str = response.json()["repo_path"]
    return repo_path


class TestOwnerParameter:
    def test_non_admin_owner_parameter_is_ignored(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        env.activate_for(PEER, GRANTED_REPO, OWN_ACTIVATION)

        repo_path = _indexes_owner(client, PEER, USER)

        assert f"/{PEER}/" in repo_path and f"/{USER}/" not in repo_path

    def test_role_admin_outside_admins_group_cannot_read_another_owner(
        self, client, env
    ):
        """ADMIN role is not the access model's admin (admins group)."""
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        env.activate_for(SWEEPER, GRANTED_REPO, OWN_ACTIVATION)

        repo_path = _indexes_owner(client, SWEEPER, USER)

        assert f"/{SWEEPER}/" in repo_path and f"/{USER}/" not in repo_path

    def test_admins_group_member_reads_another_owner(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        repo_path = _indexes_owner(client, ADMIN, USER)

        assert f"/{USER}/" in repo_path


class TestRefusedLikeUnknown:
    @pytest.mark.parametrize("route", _PRINCIPAL)
    def test_revoked_source_grant_is_refused_as_unknown(self, client, env, route):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)

        assert_refused_as_unknown(client, USER, PROBES[route], OWN_ACTIVATION)

    @pytest.mark.parametrize("route", _PRINCIPAL)
    def test_alias_naming_granted_repo_sourced_from_ungranted_is_refused(
        self, client, env, route
    ):
        """The alias is a granted golden name, but the caller's activation
        under it holds an ungranted repository."""
        env.activate_for(USER, UNGRANTED_REPO, GRANTED_REPO_2)

        assert_refused_as_unknown(client, USER, PROBES[route], GRANTED_REPO_2)

    @pytest.mark.parametrize("route", _PRINCIPAL)
    def test_another_users_alias_is_refused_as_unknown(self, client, env, route):
        env.activate_for(PEER, GRANTED_REPO, PEER_ACTIVATION)

        assert_refused_as_unknown(client, USER, PROBES[route], PEER_ACTIVATION)

    def test_composite_with_one_ungranted_source_is_refused(self, client, env):
        activate_composite(env, USER)
        probe = PROBES["repo_files"]
        assert call(client, USER, probe, COMPOSITE_ALIAS).status_code == 200
        env.group_manager.revoke_repo_access(GRANTED_REPO_2, env.group_id)

        assert_refused_as_unknown(client, USER, probe, COMPOSITE_ALIAS)

    @pytest.mark.parametrize("route", sorted(SWEEP))
    def test_every_activated_repo_route_refuses_a_revoked_activation(
        self, client, env, route
    ):
        env.activate_for(SWEEPER, GRANTED_REPO, OWN_ACTIVATION)
        env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)

        assert_refused_as_unknown(client, SWEEPER, SWEEP[route], OWN_ACTIVATION)


class TestAdminBypass:
    @pytest.mark.parametrize("route", _PRINCIPAL)
    def test_admin_activation_of_ungranted_repo_is_served(self, client, env, route):
        env.activate_for(ADMIN, UNGRANTED_REPO, OWN_ACTIVATION)

        response = call(client, ADMIN, PROBES[route], OWN_ACTIVATION)

        assert response.status_code == PROBES[route][3], response.text


class TestDeactivationStaysAvailable:
    def test_revoked_activation_can_still_be_deactivated(self, client, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        env.group_manager.revoke_repo_access(GRANTED_REPO, env.group_id)

        response = call(
            client, USER, ("DELETE", "/api/repos/{a}", {}, 202), OWN_ACTIVATION
        )

        assert response.status_code == 202, response.text


_LOOKUP_FAILURE = "example activation lookup failure detail"


class TestFailsClosed:
    @pytest.mark.parametrize("route", ["git_cat", "repo_files", "activated_branches"])
    def test_lookup_failure_refuses_generically_and_logs(
        self, client, env, monkeypatch, caplog, route
    ):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        def _broken_lookup(user_id: str) -> Any:
            raise RuntimeError(_LOOKUP_FAILURE)

        monkeypatch.setattr(
            env.access_service, "caller_activation_sources", _broken_lookup
        )
        with caplog.at_level(logging.ERROR, logger=_GUARD_LOGGER):
            response = call(client, USER, PROBES[route], OWN_ACTIVATION)

        assert response.status_code == 500, response.text
        assert response.json() == {
            "detail": {
                "error_code": "access_control_unavailable",
                "detail": "Repository access could not be verified",
            }
        }
        assert _LOOKUP_FAILURE not in response.text
        assert any(
            r.name == _GUARD_LOGGER and r.levelno == logging.ERROR
            for r in caplog.records
        ), caplog.records


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TestRunsOffTheEventLoop:
    def test_async_route_looks_up_activations_off_the_event_loop(
        self, client, env, monkeypatch
    ):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        real_lookup = env.access_service.caller_activation_sources
        on_loop: List[bool] = []

        def _recording_lookup(user_id: str) -> Any:
            on_loop.append(_on_event_loop())
            return real_lookup(user_id)

        monkeypatch.setattr(
            env.access_service, "caller_activation_sources", _recording_lookup
        )

        response = call(client, USER, PROBES["activated_branches"], OWN_ACTIVATION)

        assert response.status_code == 200, response.text
        assert on_loop == [False], on_loop
