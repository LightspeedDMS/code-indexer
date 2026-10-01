"""Audit rows for the doors the catalog closeout found writing none.

Each door is driven through its real front door (REST routes, Web routers
and the MCP ``tools/call`` dispatcher of ``_audit_front_doors``) into a real
bound audit store, and must write exactly one ``success`` or ``failure`` row
naming the acting user (or, for the loopback-only maintenance switch, the
fixed system actor) and only verified identifiers.

Doubles, beyond the harness's own: the process restart itself (a recorder
standing in for the delayed re-exec, which would end the test process).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List

import pytest
from fastapi.testclient import TestClient

from _audit_accounts_support import CAPTURE_LOGGER, capture_errors
from _audit_front_doors import ACTING_ADMIN, front_door_env

_MEMBER = "example-member"
_REPO = "example-shared-repo"


@pytest.fixture()
def doors(tmp_path: Path, monkeypatch) -> Iterator[SimpleNamespace]:
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )
    from code_indexer.server.routers import groups as groups_router
    from code_indexer.server.routers import maintenance_router
    from code_indexer.server.services import maintenance_service

    for env in front_door_env(tmp_path, monkeypatch):
        app: Any = env.client.app
        activated = ActivatedRepoManager(
            data_dir=str(tmp_path / "activated-data"),
            golden_repo_manager=env.manager,
            background_job_manager=env.jobs,  # type: ignore[arg-type]
        )
        # Both door resolutions of the activated-repo manager (REST via the
        # golden-repo manager, Web via app state) see this one instance.
        monkeypatch.setattr(
            env.manager, "activated_repo_manager", activated, raising=False
        )
        monkeypatch.setattr(
            app_module.app.state, "activated_repo_manager", activated, raising=False
        )
        groups = app_module.app.state.group_manager
        monkeypatch.setattr(groups_router, "_group_manager", groups)
        app.include_router(groups_router.router)
        app.include_router(groups_router.users_router)
        app.include_router(maintenance_router.router)
        # The maintenance switch is loopback-only: this client's peer is 127.0.0.1.
        loopback = TestClient(
            app, client=("127.0.0.1", 50000), raise_server_exceptions=False
        )
        maintenance_service._reset_maintenance_state()
        try:
            yield SimpleNamespace(
                env=env, groups=groups, loopback=loopback, activated=activated
            )
        finally:
            maintenance_service._reset_maintenance_state()


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    """No door may emit on the event loop, drop a row or build a bad event."""
    import logging

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


# =========================================================================
# Groups: membership and single-repository revoke
# =========================================================================


class TestGroupDoors:
    def test_rest_group_members_door_records_user_group_change(self, doors) -> None:
        env = doors.env
        users = doors.groups.get_group_by_name("users")
        resp = env.rest(
            "POST", f"/api/v1/groups/{users.id}/members", json={"user_id": _MEMBER}
        )
        assert resp.status_code == 200, resp.text
        missing = env.rest(
            "POST", "/api/v1/groups/987654/members", json={"user_id": _MEMBER}
        )
        assert missing.status_code == 404, missing.text

        rows = env.rows("user_group_change")
        assert [(r.actor, r.source, r.outcome) for r in rows] == [
            (ACTING_ADMIN, "rest", "success"),
            (ACTING_ADMIN, "rest", "failure"),
        ]
        assert rows[0].target_id == _MEMBER
        assert rows[0].details["to_group"] == "users"
        assert rows[1].target_id == "(unknown)"
        assert doors.groups.get_user_group(_MEMBER).name == "users"

    def test_rest_single_repo_revoke_door_records_repo_access_revoke(
        self, doors
    ) -> None:
        env = doors.env
        group = doors.groups.create_group(name="example-group", description="")
        doors.groups.grant_repo_access(_REPO, group.id, "setup")
        path = f"/api/v1/groups/{group.id}/repos/{_REPO}"
        assert env.rest("DELETE", path).status_code == 204
        assert env.rest("DELETE", path).status_code == 404

        rows = env.rows("repo_access_revoke")
        assert [(r.actor, r.source, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, "rest", "success", _REPO),
            (ACTING_ADMIN, "rest", "failure", "unresolved"),
        ]
        assert rows[0].details == {"repo": _REPO, "group": "example-group"}
        assert _REPO not in doors.groups.get_group_repos(group.id)

    def test_every_membership_door_writes_the_same_row(self, doors) -> None:
        env, groups = doors.env, doors.groups
        users = groups.get_group_by_name("users")
        power = groups.get_group_by_name("powerusers")
        groups.assign_user_to_group(_MEMBER, users.id, "setup")
        moved = env.rest(
            "PUT", f"/api/v1/users/{_MEMBER}/group", json={"group_id": power.id}
        )
        assert moved.status_code == 200, moved.text
        added = env.mcp(
            "manage_group_members",
            {"action": "add", "group_id": str(users.id), "user_id": _MEMBER},
        )
        assert added["success"] is True, added
        env.web(
            "POST", f"/admin/groups/users/{_MEMBER}/assign", data={"group_id": power.id}
        )

        rows = env.rows("user_group_change")
        assert [(r.actor, r.source, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, "rest", "success", _MEMBER),
            (ACTING_ADMIN, "mcp", "success", _MEMBER),
            (ACTING_ADMIN, "web", "success", _MEMBER),
        ]
        assert [r.details for r in rows] == [
            {"from_group": "users", "to_group": "powerusers"},
            {"from_group": "powerusers", "to_group": "users"},
            {"from_group": "users", "to_group": "powerusers"},
        ]

    def test_every_single_and_bulk_revoke_door_writes_the_same_row(
        self, doors, monkeypatch
    ) -> None:
        from code_indexer.server.web import routes as web_routes

        env, groups = doors.env, doors.groups
        group = groups.create_group(name="example-group", description="")
        repos = [f"example-repo-{n}" for n in range(5)]
        for repo in repos:
            groups.grant_repo_access(repo, group.id, "setup")
        monkeypatch.setattr(web_routes, "get_csrf_token_from_cookie", lambda _r: "x")

        base = f"/api/v1/groups/{group.id}/repos"
        assert env.rest("DELETE", f"{base}/{repos[0]}").status_code == 204
        bulk = env.rest("DELETE", base, json={"repos": [repos[1]]})
        assert bulk.status_code == 200, bulk.text
        single = {"action": "remove", "group_id": str(group.id), "repos": [repos[2]]}
        assert env.mcp("manage_group_repos", single)["success"] is True
        many = {"action": "bulk_remove", "group_id": str(group.id), "repos": [repos[3]]}
        assert env.mcp("manage_group_repos", many)["success"] is True
        env.web(
            "POST",
            "/admin/groups/repo-access/revoke",
            data={"repo_name": repos[4], "group_id": group.id, "csrf_token": "x"},
        )

        rows = env.rows("repo_access_revoke")
        assert [(r.actor, r.source, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, source, "success", repo)
            for source, repo in zip(("rest", "rest", "mcp", "mcp", "web"), repos)
        ]
        assert [r.details for r in rows] == [
            {"repo": repo, "group": "example-group"} for repo in repos
        ]
        assert set(groups.get_group_repos(group.id)).isdisjoint(repos)

    def test_a_bulk_revoke_summarises_absent_repos_in_one_failure_row(
        self, doors
    ) -> None:
        from code_indexer.server.services.audit_log_query import details_read_schema

        env, groups = doors.env, doors.groups
        group = groups.create_group(name="example-group", description="")
        for repo in ("example-rest-present", "example-mcp-present"):
            groups.grant_repo_access(repo, group.id, "setup")
        absent = [f"example-absent-{n}" for n in range(3)]
        rest = env.rest(
            "DELETE",
            f"/api/v1/groups/{group.id}/repos",
            json={"repos": ["example-rest-present", *absent]},
        )
        assert rest.status_code == 200, rest.text
        args = {
            "action": "bulk_remove",
            "group_id": str(group.id),
            "repos": ["example-mcp-present", *absent],
        }
        assert env.mcp("manage_group_repos", args)["removed_count"] == 1

        rows = env.rows("repo_access_revoke")
        assert [(r.source, r.outcome, r.target_id) for r in rows] == [
            ("rest", "success", "example-rest-present"),
            ("rest", "failure", "unresolved"),
            ("mcp", "success", "example-mcp-present"),
            ("mcp", "failure", "unresolved"),
        ]
        summary = {"group": "example-group", "not_in_group_count": 3}
        assert [rows[1].details, rows[3].details] == [summary, summary]
        assert set(summary) <= set(details_read_schema("repo_access_revoke"))

    def test_mcp_doors_with_an_unknown_group_record_failure_and_keep_their_error(
        self, doors
    ) -> None:
        env = doors.env
        unknown = {"group_id": "987654"}
        refused = {"success": False, "error": "Group not found: 987654"}
        member = {"action": "add", "user_id": _MEMBER, **unknown}
        assert env.mcp("manage_group_members", member) == refused
        for action in ("remove", "bulk_remove"):
            args = {"action": action, "repos": [_REPO], **unknown}
            assert env.mcp("manage_group_repos", args) == refused

        member_rows = env.rows("user_group_change")
        assert [(r.source, r.outcome, r.target_id) for r in member_rows] == [
            ("mcp", "failure", "(unknown)")
        ]
        revoke_rows = env.rows("repo_access_revoke")
        assert [(r.source, r.outcome, r.target_id) for r in revoke_rows] == [
            ("mcp", "failure", "unresolved"),
            ("mcp", "failure", "unresolved"),
        ]


# =========================================================================
# Maintenance mode (loopback-only switch driven by the local auto-updater)
# =========================================================================

_MAINTENANCE_ACTOR = "system:localhost-maintenance"


class TestMaintenanceDoors:
    def test_loopback_enter_and_exit_record_the_system_actor(self, doors) -> None:
        env = doors.env
        headers = {"Authorization": f"Bearer {env.bearer}"}
        for step in ("enter", "exit"):
            resp = doors.loopback.post(
                f"/api/admin/maintenance/{step}", headers=headers
            )
            assert resp.status_code == 200, resp.text

        rows = [
            *env.rows("maintenance_mode_entered"),
            *env.rows("maintenance_mode_exited"),
        ]
        assert [
            (r.action_type, r.actor, r.actor_is_system, r.outcome, r.auth_method)
            for r in rows
        ] == [
            ("maintenance_mode_entered", _MAINTENANCE_ACTOR, 1, "success", "system"),
            ("maintenance_mode_exited", _MAINTENANCE_ACTOR, 1, "success", "system"),
        ]
        assert {(r.target_type, r.target_id) for r in rows} == {("server", "node")}
        assert [r.details for r in rows] == [{"origin": "loopback"}] * 2

    def test_a_refused_remote_caller_changes_nothing_and_records_nothing(
        self, doors
    ) -> None:
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        env = doors.env
        assert env.rest("POST", "/api/admin/maintenance/enter").status_code == 403
        assert get_maintenance_state().is_maintenance_mode() is False
        assert env.rows("maintenance_mode_entered") == []


# =========================================================================
# Admin-triggered server restart
# =========================================================================


class TestRestartDoor:
    def test_restart_records_the_admin_before_the_restart_is_triggered(
        self, doors, tmp_path, monkeypatch
    ) -> None:
        from code_indexer.server.services import config_service as config_module
        from code_indexer.server.web import routes as web_routes

        env = doors.env
        monkeypatch.setattr(config_module, "LAUNCH_CONFIG_PATH", tmp_path / "l.json")
        seen_at_trigger: List[List[Any]] = []
        monkeypatch.setattr(
            web_routes,
            "_schedule_delayed_restart",
            lambda delay=2: seen_at_trigger.append(
                env.rows("server_restart_requested")
            ),
        )
        monkeypatch.setattr(web_routes, "_restart_in_progress", False)

        resp = env.web("POST", "/admin/restart", headers={"X-CSRF-Token": "x"})
        assert resp.status_code == 202, resp.text

        assert len(seen_at_trigger) == 1
        rows = env.rows("server_restart_requested")
        assert seen_at_trigger[0] == rows
        assert [
            (r.actor, r.source, r.outcome, r.target_type, r.target_id) for r in rows
        ] == [(ACTING_ADMIN, "web", "success", "server", "node")]


# =========================================================================
# Activated repositories an admin manages for another user
# =========================================================================

_TARGET = "example-target-user"


def _leftover_copy(activated: Any, username: str, alias: str) -> None:
    """An on-disk activated copy (deactivation's orphan-cleanup path)."""
    Path(activated.activated_repos_dir, username, alias).mkdir(parents=True)


class TestActivatedRepoDoors:
    def test_web_activate_for_another_user_names_admin_and_target(self, doors) -> None:
        from _audit_repos_support import EXAMPLE_ALIAS

        env = doors.env
        form = {"golden_alias": EXAMPLE_ALIAS, "username": _TARGET}
        env.web(
            "POST",
            "/admin/golden-repos/activate",
            data={**form, "user_alias": "example-copy"},
        )
        env.web(
            "POST",
            "/admin/golden-repos/activate",
            data={**form, "golden_alias": "example-missing"},
        )

        rows = env.rows("user_repo_activated_by_admin")
        assert [(r.actor, r.source, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, "web", "success", _TARGET),
            (ACTING_ADMIN, "web", "failure", "(unknown)"),
        ]
        submission = env.jobs.submissions[-1]
        assert (submission["operation_type"], submission["username"]) == (
            "activate_repository",
            _TARGET,
        )
        assert rows[0].details == {
            "user_alias": "example-copy",
            "golden_repo_alias": EXAMPLE_ALIAS,
            "job_id": submission["job_id"],
        }
        assert rows[1].details == {}

    def test_both_admin_deactivate_doors_write_the_same_row(self, doors) -> None:
        env = doors.env
        for alias in ("example-a", "example-b"):
            _leftover_copy(doors.activated, _TARGET, alias)
        rest = env.rest("DELETE", f"/api/admin/activated-repos/{_TARGET}/example-a")
        assert rest.status_code == 202, rest.text
        env.web("POST", f"/admin/repos/{_TARGET}/example-b/deactivate")
        absent = env.rest("DELETE", f"/api/admin/activated-repos/{_TARGET}/example-x")
        assert absent.status_code == 404, absent.text

        rows = env.rows("user_repo_deactivated_by_admin")
        assert [(r.actor, r.source, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, "rest", "success", _TARGET),
            (ACTING_ADMIN, "web", "success", _TARGET),
            (ACTING_ADMIN, "rest", "failure", "(unknown)"),
        ]
        jobs = [s["job_id"] for s in env.jobs.submissions]
        assert [r.details for r in rows] == [
            {"user_alias": "example-a", "job_id": jobs[0]},
            {"user_alias": "example-b", "job_id": jobs[1]},
            {},
        ]

    def test_an_admin_deactivating_their_own_copy_writes_no_admin_row(
        self, doors
    ) -> None:
        env = doors.env
        _leftover_copy(doors.activated, ACTING_ADMIN, "example-own")
        path = f"/api/admin/activated-repos/{ACTING_ADMIN}/example-own"
        assert env.rest("DELETE", path).status_code == 202
        assert len(env.jobs.submissions) == 1
        assert env.rows("user_repo_deactivated_by_admin") == []

    def test_an_admin_activating_a_copy_for_themselves_writes_no_admin_row(
        self, doors
    ) -> None:
        from _audit_repos_support import EXAMPLE_ALIAS

        env = doors.env
        form = {"golden_alias": EXAMPLE_ALIAS, "username": ACTING_ADMIN}
        env.web("POST", "/admin/golden-repos/activate", data=form)
        assert [s["username"] for s in env.jobs.submissions] == [ACTING_ADMIN]
        assert env.rows("user_repo_activated_by_admin") == []


# =========================================================================
# Failure paths: one failure row, the original error still propagates
# =========================================================================


class _SwitchBroken(RuntimeError):
    pass


def _raise_broken(*_args: Any, **_kwargs: Any) -> Any:
    raise _SwitchBroken("switch failed")


class TestFailurePaths:
    def test_a_failing_maintenance_switch_records_failure_and_propagates(
        self, doors, monkeypatch
    ) -> None:
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        env = doors.env
        monkeypatch.setattr(
            get_maintenance_state(), "enter_maintenance_mode", _raise_broken
        )
        resp = doors.loopback.post(
            "/api/admin/maintenance/enter",
            headers={"Authorization": f"Bearer {env.bearer}"},
        )
        assert resp.status_code == 500
        rows = env.rows("maintenance_mode_entered")
        assert [(r.actor, r.outcome, r.details) for r in rows] == [
            (_MAINTENANCE_ACTOR, "failure", {"origin": "loopback"})
        ]

    def test_a_failing_restart_trigger_follows_the_request_row_with_failure(
        self, doors, tmp_path, monkeypatch
    ) -> None:
        from code_indexer.server.services import config_service as config_module
        from code_indexer.server.web import routes as web_routes

        env = doors.env
        monkeypatch.setattr(config_module, "LAUNCH_CONFIG_PATH", tmp_path / "l.json")
        monkeypatch.setattr(web_routes, "_schedule_delayed_restart", _raise_broken)
        monkeypatch.setattr(web_routes, "_restart_in_progress", False)
        resp = env.web("POST", "/admin/restart", headers={"X-CSRF-Token": "x"})
        assert resp.status_code == 500
        rows = env.rows("server_restart_requested")
        assert [(r.actor, r.outcome) for r in rows] == [
            (ACTING_ADMIN, "success"),
            (ACTING_ADMIN, "failure"),
        ]

    def test_a_refused_cidx_meta_revoke_records_failure(self, doors) -> None:
        env = doors.env
        users = doors.groups.get_group_by_name("users")
        resp = env.rest("DELETE", f"/api/v1/groups/{users.id}/repos/cidx-meta")
        assert resp.status_code == 400, resp.text
        rows = env.rows("repo_access_revoke")
        assert [(r.actor, r.outcome, r.target_id) for r in rows] == [
            (ACTING_ADMIN, "failure", "unresolved")
        ]

    def test_an_audit_write_that_raises_never_blocks_the_change(
        self, doors, monkeypatch
    ) -> None:
        env, groups = doors.env, doors.groups
        users = groups.get_group_by_name("users")
        monkeypatch.setattr(groups, "log_audit", _raise_broken)
        resp = env.rest(
            "POST", f"/api/v1/groups/{users.id}/members", json={"user_id": _MEMBER}
        )
        assert resp.status_code == 200, resp.text
        assert groups.get_user_group(_MEMBER).name == "users"
        assert env.rows("user_group_change") == []
