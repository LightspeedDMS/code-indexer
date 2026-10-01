"""Removing another user's activated repository requires elevation on every door.

The REST and Web admin doors apply the same rule: with elevation enforcement
on, the admin must hold an active elevation window of their own; without one
the request is refused and no deactivation job is submitted.

Harness: ``_audit_front_doors`` (real auth components, real managers) with a
real ActivatedRepoManager and elevation enforcement switched ON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from _audit_front_doors import ACTING_ADMIN, DoorsEnv, front_door_env
from tests.unit.server.self_service_elevation_harness import enforcement

_TARGET = "example-target-user"
_ALIAS = "example-copy"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    for doors in front_door_env(tmp_path, monkeypatch):
        activated = ActivatedRepoManager(
            data_dir=str(tmp_path / "activated-data"),
            golden_repo_manager=doors.manager,
            background_job_manager=doors.jobs,  # type: ignore[arg-type]
        )
        monkeypatch.setattr(
            doors.manager, "activated_repo_manager", activated, raising=False
        )
        monkeypatch.setattr(
            app_module.app.state, "activated_repo_manager", activated, raising=False
        )
        # An on-disk copy of the target user's repository to remove.
        Path(activated.activated_repos_dir, _TARGET, _ALIAS).mkdir(parents=True)
        doors.stack.enroll_mfa(ACTING_ADMIN)
        with enforcement(True):
            yield doors


def _rest(env: DoorsEnv):
    return env.rest("DELETE", f"/api/admin/activated-repos/{_TARGET}/{_ALIAS}")


def _web(env: DoorsEnv):
    return env.web("POST", f"/admin/repos/{_TARGET}/{_ALIAS}/deactivate")


class TestRest:
    def test_refused_without_an_elevation_window(self, env) -> None:
        resp = _rest(env)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "elevation_required"
        assert env.jobs.submissions == []

    def test_a_non_admin_is_refused(self, env) -> None:
        from code_indexer.server.auth.user_manager import UserRole

        member = env.stack.user_manager.create_user(
            "example-member", "SecureP@ssw0rd!XyZ789", UserRole.NORMAL_USER
        )
        bearer, _jti = env.stack.bearer(member)
        resp = env.client.delete(
            f"/api/admin/activated-repos/{_TARGET}/{_ALIAS}",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 403, resp.text
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env) -> None:
        jti = str(env.stack.jwt_manager.validate_token(env.bearer)["jti"])
        env.stack.elevate(jti, ACTING_ADMIN)
        resp = _rest(env)
        assert resp.status_code == 202, resp.text
        assert len(env.jobs.submissions) == 1


class TestWeb:
    def test_refused_without_an_elevation_window(self, env) -> None:
        assert _web(env).status_code == 403
        assert env.jobs.submissions == []

    def test_allowed_with_the_callers_window(self, env) -> None:
        env.stack.elevate(env.cookie, ACTING_ADMIN)
        _web(env)
        assert len(env.jobs.submissions) == 1
