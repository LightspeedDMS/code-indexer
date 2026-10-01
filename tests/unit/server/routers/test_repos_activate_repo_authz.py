"""POST /api/repos/activate and GET /api/repos/golden/*: activation and
golden-repository reads require group access to every golden repository
named in the request, checked before any activation job is submitted.

Front door: a real FastAPI TestClient over the real routes, wired to the
REAL services in repo_authz_test_env (nothing about the access decision is
mocked). "No job submitted" is asserted on the submissions recorded by the
env's job runner (RecordingJobManager), not inferred from the status code.
"""

from __future__ import annotations

import pytest

from tests.unit.server.routers.repo_authz_test_env import (
    ADMIN_USERNAME,
    GHOST_REPO,
    GRANTED_REPO,
    POWER_USERNAME,
    UNGRANTED_REPO,
    admin as _admin,
    env,  # noqa: F401  (pytest fixture)
    power_user as _power_user,
)


class TestActivationRequiresGroupAccess:
    def test_ungranted_single_alias_is_refused_and_submits_no_job(self, env):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.post(
            "/api/repos/activate", json={"golden_repo_alias": UNGRANTED_REPO}
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert UNGRANTED_REPO in detail["detail"]
        assert env.submitted_jobs() == []

    def test_ungranted_alias_with_branch_is_refused_and_submits_no_job(self, env):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.post(
            "/api/repos/activate",
            json={
                "golden_repo_alias": UNGRANTED_REPO,
                "branch_name": "main",
                "user_alias": "my-copy",
            },
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"
        assert env.submitted_jobs() == []

    def test_composite_with_any_ungranted_alias_is_refused_and_submits_no_job(
        self,
        env,  # noqa: F811
    ):
        client = env.client(_power_user(), env.access_service)
        resp = client.post(
            "/api/repos/activate",
            json={
                "golden_repo_aliases": [GRANTED_REPO, UNGRANTED_REPO],
                "user_alias": "my-composite",
            },
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert UNGRANTED_REPO in detail["detail"]
        assert env.submitted_jobs() == []

    def test_unknown_and_ungranted_aliases_get_identical_refusal(self, env):  # noqa: F811
        """Existence of a repository is not revealed to a caller without
        access: an unknown alias and a real ungranted one are refused alike."""
        client = env.client(_power_user(), env.access_service)
        real = client.post(
            "/api/repos/activate", json={"golden_repo_alias": UNGRANTED_REPO}
        )
        unknown = client.post(
            "/api/repos/activate", json={"golden_repo_alias": "no-such-repo"}
        )

        assert real.status_code == unknown.status_code == 403
        assert real.json()["detail"]["error_code"] == "access_denied"
        assert unknown.json()["detail"]["error_code"] == "access_denied"
        assert env.submitted_jobs() == []

    def test_granted_user_activation_is_submitted(self, env):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.post(
            "/api/repos/activate", json={"golden_repo_alias": GRANTED_REPO}
        )

        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        jobs = env.submitted_jobs()
        assert [j["job_id"] for j in jobs] == [job_id]
        assert jobs[0]["operation_type"] == "activate_repository"
        assert jobs[0]["user"] == POWER_USERNAME
        assert jobs[0]["repo_alias"] == GRANTED_REPO

    def test_admin_activation_is_submitted_without_group_grant(self, env):  # noqa: F811
        client = env.client(_admin(), env.access_service)
        resp = client.post(
            "/api/repos/activate", json={"golden_repo_alias": UNGRANTED_REPO}
        )

        assert resp.status_code == 202, resp.text
        jobs = env.submitted_jobs()
        assert [j["job_id"] for j in jobs] == [resp.json()["job_id"]]
        assert jobs[0]["operation_type"] == "activate_repository"
        assert jobs[0]["user"] == ADMIN_USERNAME
        assert jobs[0]["repo_alias"] == UNGRANTED_REPO

    def test_admin_composite_activation_is_submitted(self, env):  # noqa: F811
        client = env.client(_admin(), env.access_service)
        resp = client.post(
            "/api/repos/activate",
            json={
                "golden_repo_aliases": [GRANTED_REPO, UNGRANTED_REPO],
                "user_alias": "admin-composite",
            },
        )

        assert resp.status_code == 202, resp.text
        jobs = env.submitted_jobs()
        assert [j["operation_type"] for j in jobs] == ["activate_composite_repository"]
        assert [j["job_id"] for j in jobs] == [resp.json()["job_id"]]
        assert jobs[0]["user"] == ADMIN_USERNAME
        assert jobs[0]["repo_alias"] == "admin-composite"
        assert env.job_manager.submissions[0]["golden_repo_aliases"] == [
            GRANTED_REPO,
            UNGRANTED_REPO,
        ]

    @pytest.mark.parametrize("make_user", [_power_user, _admin])
    def test_unavailable_access_service_fails_closed_and_submits_no_job(
        self,
        env,  # noqa: F811
        make_user,
    ):
        client = env.client(make_user(), None)
        resp = client.post(
            "/api/repos/activate", json={"golden_repo_alias": GRANTED_REPO}
        )

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
        assert env.submitted_jobs() == []


class TestActivationNotFoundListsOnlyAccessibleRepos:
    """The activation "not found" suggestions list only repositories the
    caller has access to; admins see every golden repository."""

    def test_non_admin_suggestions_hold_only_accessible_repos(self, env):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.post(
            "/api/repos/activate", json={"golden_repo_alias": GHOST_REPO}
        )

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["available_repositories"] == [GRANTED_REPO]
        assert env.submitted_jobs() == []

    def test_admin_suggestions_hold_every_golden_repo(self, env):  # noqa: F811
        client = env.client(_admin(), env.access_service)
        resp = client.post(
            "/api/repos/activate", json={"golden_repo_alias": GHOST_REPO}
        )

        assert resp.status_code == 404, resp.text
        assert sorted(resp.json()["detail"]["available_repositories"]) == sorted(
            [GRANTED_REPO, UNGRANTED_REPO]
        )
        assert env.submitted_jobs() == []


GOLDEN_READ_ROUTES = [
    "/api/repos/golden/{alias}",
    "/api/repos/golden/{alias}/branches",
]


@pytest.mark.parametrize("route", GOLDEN_READ_ROUTES)
class TestGoldenRepositoryReadsRequireGroupAccess:
    """Reading a golden repository's details or branches requires group
    access to it, with the same decision as the MCP door."""

    def test_ungranted_user_is_refused(self, env, route):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.get(route.format(alias=UNGRANTED_REPO))

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "access_denied"
        assert UNGRANTED_REPO in detail["detail"]

    def test_unknown_alias_is_refused_like_an_ungranted_one(self, env, route):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.get(route.format(alias="no-such-repo"))

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"

    def test_granted_user_is_served(self, env, route):  # noqa: F811
        client = env.client(_power_user(), env.access_service)
        resp = client.get(route.format(alias=GRANTED_REPO))

        assert resp.status_code == 200, resp.text

    def test_admin_is_served_without_group_grant(self, env, route):  # noqa: F811
        client = env.client(_admin(), env.access_service)
        resp = client.get(route.format(alias=UNGRANTED_REPO))

        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("make_user", [_power_user, _admin])
    def test_unavailable_access_service_fails_closed(self, env, route, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.get(route.format(alias=GRANTED_REPO))

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
