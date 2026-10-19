# ruff: noqa: F811
"""REST: only the admins group may activate the internal catalog repository,
or use an activated copy of it.

Driven through the real REST front door of a real app (``create_app`` via
``isolated_app``, never ~/.cidx-server) over the real services
(QueryAccessEnv). A refused activation answers exactly what the route
answers for an ungranted repository; a refused existing copy answers
exactly what the route answers for an alias the caller never activated.

Repository aliases and usernames are neutral placeholders.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict

import pytest

from code_indexer.server.services.constants import CIDX_META_REPO
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    CATALOG_FILE,
    CATALOG_TEXT,
    GRANTED_REPO,
    UNGRANTED_REPO,
    USER,
    register_catalog,
)
from tests.unit.server.routers.activated_repo_access_env import (  # noqa: F401
    SWEEPER,
    Probe,
    assert_refused_as_unknown,
    call,
    client,
    env,
    server_db_template,
    shape,
)

META_COPY = "my-catalog"
ADMIN_META_COPY = "admin-catalog"

CONTENT: Dict[str, Probe] = {
    "v2_files": ("GET", "/api/repositories/{a}/files", {}, 200),
    "v2_file_content": (
        "GET",
        "/api/repositories/{a}/files",
        {"params": {"content": "true", "path": CATALOG_FILE}},
        200,
    ),
    "git_cat": (
        "GET",
        "/api/v1/repos/{a}/git/cat",
        {"params": {"path": CATALOG_FILE}},
        200,
    ),
    "git_log": ("GET", "/api/v1/repos/{a}/git/log", {}, 200),
    "git_blame": (
        "GET",
        "/api/v1/repos/{a}/git/blame",
        {"params": {"path": CATALOG_FILE}},
        200,
    ),
    "repo_files": ("GET", "/api/repos/{a}/files", {}, 200),
    "health_indexes": ("GET", "/api/repositories/{a}/indexes", {}, 200),
    "health_check": ("POST", "/api/repositories/{a}/health/check", {}, 202),
}
HEALTH_ROUTES = ("health_check", "health_indexes")


@pytest.fixture
def catalog(env, monkeypatch):
    register_catalog(env)
    stand_in = vars(importlib.import_module("code_indexer.server.app"))["app"]
    monkeypatch.setattr(
        stand_in.state, "golden_repo_manager", env.golden_repo_manager, raising=False
    )
    return env


def _activate(client: Any, username: str, body: Dict[str, Any]) -> Any:
    return call(
        client, username, ("POST", "/api/repos/activate", {"json": body}, 0), ""
    )


def _activations(env: Any, username: str) -> list:
    return [
        a["user_alias"]
        for a in env.activated_repo_manager.list_activated_repositories(username)
    ]


class TestActivationRefused:
    @pytest.mark.parametrize("caller", [USER, SWEEPER])
    def test_non_admin_activation_is_refused_like_an_ungranted_repo(
        self, client, catalog, caller
    ):
        meta = _activate(
            client, caller, {"golden_repo_alias": CIDX_META_REPO, "user_alias": "c"}
        )
        ungranted = _activate(
            client, caller, {"golden_repo_alias": UNGRANTED_REPO, "user_alias": "c"}
        )

        assert meta.status_code == 403, meta.text
        assert shape(meta, CIDX_META_REPO) == shape(ungranted, UNGRANTED_REPO)
        assert _activations(catalog, caller) == []

    def test_composite_containing_the_catalog_is_refused(self, client, catalog):
        response = _activate(
            client,
            USER,
            {
                "golden_repo_aliases": [GRANTED_REPO, CIDX_META_REPO],
                "user_alias": "my-composite",
            },
        )

        assert response.status_code == 403, response.text
        assert _activations(catalog, USER) == []


class TestActivationAllowed:
    def test_admin_activation_of_the_catalog_succeeds(self, client, catalog):
        response = _activate(
            client,
            ADMIN,
            {"golden_repo_alias": CIDX_META_REPO, "user_alias": ADMIN_META_COPY},
        )

        assert response.status_code == 202, response.text
        status = catalog.wait_for_job(response.json()["job_id"], ADMIN)
        assert status["status"] == "completed", status

    def test_activation_of_a_granted_repo_is_unaffected(self, client, catalog):
        response = _activate(
            client, USER, {"golden_repo_alias": GRANTED_REPO, "user_alias": "mine"}
        )

        assert response.status_code == 202, response.text


class TestExistingCopy:
    @pytest.mark.parametrize("route", sorted(CONTENT))
    def test_existing_copy_is_refused_like_an_unknown_alias(
        self, client, catalog, route
    ):
        catalog.activate_for(USER, CIDX_META_REPO, META_COPY)

        assert_refused_as_unknown(client, USER, CONTENT[route], META_COPY)

    def test_existing_copy_rows_are_never_returned_by_query(self, client, catalog):
        catalog.activate_for(USER, CIDX_META_REPO, META_COPY)
        query: Probe = (
            "POST",
            "/api/query",
            {"json": {"query_text": "find", "repository_alias": "{a}", "limit": 9}},
            0,
        )

        response = call(client, USER, query, META_COPY)

        assert response.status_code in (200, 403, 404), response.text
        rows = response.json().get("results", []) if response.status_code == 200 else []
        assert [r for r in rows if r["repository_alias"] == META_COPY] == []

    @pytest.mark.parametrize("route", HEALTH_ROUTES)
    def test_granted_activation_reaches_health_routes(self, client, catalog, route):
        catalog.activate_for(USER, GRANTED_REPO, "mine")

        response = call(client, USER, CONTENT[route], "mine")

        assert response.status_code == CONTENT[route][3], response.text

    def test_admins_own_copy_is_served(self, client, catalog):
        catalog.activate_for(ADMIN, CIDX_META_REPO, ADMIN_META_COPY)

        response = call(client, ADMIN, CONTENT["git_cat"], ADMIN_META_COPY)

        assert response.status_code == 200, response.text
        assert response.json()["content"].strip() == CATALOG_TEXT
