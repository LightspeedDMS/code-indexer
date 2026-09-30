"""Routes that list or look up golden repositories return only the
repositories the caller has group access to (admins see all), and refuse
with 500 access_control_unavailable when the access service is missing.

Real services from repo_authz_test_env; nothing about the access decision
is mocked.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from tests.unit.server.routers.repo_authz_test_env import (
    GRANTED_REPO,
    UNGRANTED_REPO,
    admin,
    env,  # noqa: F401  (pytest fixture)
    power_user,
    repo_url,
)

# An ADMIN-role user who is deliberately NOT a member of the admins group.
ROLE_ADMIN_USERNAME = "example_role_admin"


def _discover(client, alias: str):
    return client.get("/api/repos/discover", params={"source": repo_url(alias)})


def _golden_aliases(resp) -> list:
    return [m["alias"] for m in resp.json()["golden_repositories"]]


class TestDiscoverListsOnlyAccessibleGoldenRepos:
    def test_ungranted_repo_is_not_revealed(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = _discover(client, UNGRANTED_REPO)

        assert resp.status_code == 200, resp.text
        assert _golden_aliases(resp) == []
        assert resp.json()["total_matches"] == 0

    def test_granted_repo_is_returned(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = _discover(client, GRANTED_REPO)

        assert resp.status_code == 200, resp.text
        assert _golden_aliases(resp) == [GRANTED_REPO]

    def test_admin_sees_ungranted_repo(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = _discover(client, UNGRANTED_REPO)

        assert resp.status_code == 200, resp.text
        assert _golden_aliases(resp) == [UNGRANTED_REPO]

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = _discover(client, GRANTED_REPO)

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"


class TestGlobalReposListOnlyAccessibleRepos:
    def test_non_admin_sees_only_granted_repos(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get("/global/repos")

        assert resp.status_code == 200, resp.text
        assert [r["repo_name"] for r in resp.json()["repos"]] == [GRANTED_REPO]

    def test_admin_sees_every_repo(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = client.get("/global/repos")

        assert resp.status_code == 200, resp.text
        assert sorted(r["repo_name"] for r in resp.json()["repos"]) == sorted(
            [GRANTED_REPO, UNGRANTED_REPO]
        )

    def test_admin_role_outside_admins_group_sees_only_its_groups_repos(
        self,
        env,  # noqa: F811
    ):
        """Admin repository visibility follows membership of the admins group
        (the access model's full-access group), not the user role.

        A user with the ADMIN role who is NOT in the admins group, but is in
        a group granted exactly one golden repository, lists only that one.
        """
        gam = env.access_service.group_manager
        restricted = gam.get_group_by_name("restricted")
        assert restricted is not None
        gam.assign_user_to_group(ROLE_ADMIN_USERNAME, restricted.id, assigned_by="test")
        assert not env.access_service.is_admin_user(ROLE_ADMIN_USERNAME)
        role_admin = User(
            username=ROLE_ADMIN_USERNAME,
            password_hash="$2b$12$x",
            role=UserRole.ADMIN,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        client = env.client(role_admin, env.access_service)
        resp = client.get("/global/repos")

        assert resp.status_code == 200, resp.text
        assert [r["repo_name"] for r in resp.json()["repos"]] == [GRANTED_REPO]

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.get("/global/repos")

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"


class TestGlobalRepoStatusRequiresGroupAccess:
    def test_ungranted_user_is_refused(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get(f"/global/repos/{UNGRANTED_REPO}-global/status")

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"

    def test_unknown_alias_is_refused_like_an_ungranted_one(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get("/global/repos/no-such-repo-global/status")

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error_code"] == "access_denied"

    def test_granted_user_is_served(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get(f"/global/repos/{GRANTED_REPO}-global/status")

        assert resp.status_code == 200, resp.text
        assert resp.json()["repo_name"] == GRANTED_REPO

    def test_admin_is_served_without_group_grant(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = client.get(f"/global/repos/{UNGRANTED_REPO}-global/status")

        assert resp.status_code == 200, resp.text
        assert resp.json()["repo_name"] == UNGRANTED_REPO

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.get(f"/global/repos/{GRANTED_REPO}-global/status")

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"


class TestAvailableListsOnlyAccessibleGoldenRepos:
    def test_non_admin_sees_only_granted_repos(self, env):  # noqa: F811
        client = env.client(power_user(), env.access_service)
        resp = client.get("/api/repos/available")

        assert resp.status_code == 200, resp.text
        assert [r["alias"] for r in resp.json()["repositories"]] == [GRANTED_REPO]

    def test_admin_sees_every_repo(self, env):  # noqa: F811
        client = env.client(admin(), env.access_service)
        resp = client.get("/api/repos/available")

        assert resp.status_code == 200, resp.text
        assert sorted(r["alias"] for r in resp.json()["repositories"]) == sorted(
            [GRANTED_REPO, UNGRANTED_REPO]
        )

    @pytest.mark.parametrize("make_user", [power_user, admin])
    def test_unavailable_access_service_fails_closed(self, env, make_user):  # noqa: F811
        client = env.client(make_user(), None)
        resp = client.get("/api/repos/available")

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"]["error_code"] == "access_control_unavailable"
