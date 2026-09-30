"""/api/repositories/{repo_id}: when repo_id does not name one of the
caller's own activations, the golden repository it names is served only to
a caller with group access to it, checked before the golden lookup and
before any sync work is submitted.

Real services from repo_authz_test_env; nothing about the access decision
is mocked.
"""

from __future__ import annotations

import pytest

from tests.unit.server.routers.repo_authz_test_env import (
    ADMIN_USERNAME,
    GRANTED_REPO,
    POWER_USERNAME,
    UNGRANTED_REPO,
    admin,
    env,  # noqa: F401  (pytest fixture)
    power_user,
)

V2_READ_ROUTES = [
    "/api/repositories/{repo_id}",
    "/api/repositories/{repo_id}/branches",
]


@pytest.mark.parametrize("route", V2_READ_ROUTES)
class TestV2GoldenReadsRequireGroupAccess:
    def test_ungranted_user_is_refused(self, env, route):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get(route.format(repo_id=UNGRANTED_REPO))

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert UNGRANTED_REPO in detail["detail"]

    def test_unknown_id_is_refused_like_an_ungranted_one(self, env, route):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get(route.format(repo_id="no-such-repo"))

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"

    def test_granted_user_is_served(self, env, route):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get(route.format(repo_id=GRANTED_REPO))

        assert resp.status_code == 200, resp.text

    def test_admin_is_served_without_group_grant(self, env, route):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = client.get(route.format(repo_id=UNGRANTED_REPO))

        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, route, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.get(route.format(repo_id=GRANTED_REPO))

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"


class TestV2SyncRequiresGroupAccess:
    def test_ungranted_user_is_refused_and_submits_no_job(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.post(f"/api/repositories/{UNGRANTED_REPO}/sync")

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"
        assert env.submitted_jobs() == []

    def test_unknown_id_is_refused_and_submits_no_job(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.post("/api/repositories/no-such-repo/sync")

        assert resp.status_code == 403, resp.text
        assert env.submitted_jobs() == []

    def test_granted_user_sync_is_submitted(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.post(f"/api/repositories/{GRANTED_REPO}/sync")

        assert resp.status_code == 202, resp.text
        jobs = env.submitted_jobs()
        assert [j["job_id"] for j in jobs] == [resp.json()["job_id"]]
        assert jobs[0]["user"] == POWER_USERNAME
        assert jobs[0]["operation_type"] == "sync_repository"
        assert jobs[0]["repo_alias"] == GRANTED_REPO

    def test_admin_sync_is_submitted_without_group_grant(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = client.post(f"/api/repositories/{UNGRANTED_REPO}/sync")

        assert resp.status_code == 202, resp.text
        jobs = env.submitted_jobs()
        assert [j["user"] for j in jobs] == [ADMIN_USERNAME]
        assert [j["job_id"] for j in jobs] == [resp.json()["job_id"]]
        assert jobs[0]["operation_type"] == "sync_repository"
        assert jobs[0]["repo_alias"] == UNGRANTED_REPO

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed_and_submits_no_job(
        self,
        env,  # noqa: F811
        make_user,
    ):
        client = env.client(make_user(), None)
        resp = client.post(f"/api/repositories/{GRANTED_REPO}/sync")

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
        assert env.submitted_jobs() == []


class TestV2SearchRequiresGroupAccess:
    """Search names a global repository alias; it requires group access to
    that repository before any search work."""

    BODY = {"query": "example", "limit": 1}

    def test_ungranted_user_is_refused(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.post(
            f"/api/repositories/{UNGRANTED_REPO}-global/search", json=self.BODY
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert f"{UNGRANTED_REPO}-global" in detail["detail"]

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.post(
            f"/api/repositories/{GRANTED_REPO}-global/search", json=self.BODY
        )

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
