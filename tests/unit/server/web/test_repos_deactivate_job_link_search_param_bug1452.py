"""
Unit tests for the "Deactivating..." badge / deactivation success-message
job links -- same bug class as #1452, found by a code reviewer auditing
that fix in a DIFFERENT pair of locations.

The `jobs_page` / `jobs_list_partial` routes in `web/routes.py` bind the
search query parameter as `search` (see the route signature: `search:
Optional[str] = None`). Two locations build links using the WRONG param
name `search_text`, which FastAPI silently ignores because no such
parameter is bound:

1. `repos_list.html`'s "Deactivating..." badge:
   `/admin/jobs?search_text={{ deact_job_id }}`
2. `routes.py`'s `deactivate_repo` success-message job link:
   `<a href="/admin/jobs?search_text={job_id}">{job_id}</a>`

Fix: rename both occurrences to `search=`, matching the route's actual
bound parameter name. No `search_text` alias is added to the route -- the
sibling #1452 fix deliberately standardized on the route's real param
names rather than adding aliases, and this follows the same convention.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from jinja2 import Environment, FileSystemLoader

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app, router):
    """Override all require_elevation deps so tests can call routes without TOTP setup.

    Same helper as tests/unit/server/web/test_groups_toggle_ajax.py -- the
    deactivate_repo route is gated by `Depends(dependencies.require_elevation())`,
    a separate auth boundary from `_require_admin_session`.
    """
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None


TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
)


@pytest.fixture
def jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


# ---------------------------------------------------------------------------
# repos_list.html "Deactivating..." badge
# ---------------------------------------------------------------------------


class TestRepoListDeactivatingBadgeUsesSearchParam:
    """The 'Deactivating...' badge must link with search=, not search_text=."""

    def _render(self, jinja_env: Environment) -> str:
        template = jinja_env.get_template("partials/repos_list.html")
        repo = {
            "username": "alice",
            "user_alias": "myrepo",
            "golden_repo_alias": "golden-1",
            "category_name": "Backend",
            "category_id": 1,
            "category_priority": 1,
            "activated_at": "2026-01-01T00:00:00Z",
            "status": "active",
        }
        return template.render(
            repos=[repo],
            deactivating_map={("alice", "myrepo"): "job-abc-123"},
        )

    def test_badge_does_not_use_search_text_param(self, jinja_env):
        rendered = self._render(jinja_env)
        assert "search_text=" not in rendered, (
            "repos_list.html's 'Deactivating...' badge must not link with "
            "'?search_text=' -- the jobs_page route only binds 'search', "
            "so this param name is silently ignored by FastAPI"
        )

    def test_badge_links_with_search_param(self, jinja_env):
        rendered = self._render(jinja_env)
        assert "/admin/jobs?search=job-abc-123" in rendered, (
            "Expected 'Deactivating...' badge href "
            "'/admin/jobs?search=job-abc-123' in repos_list.html rendered "
            "output"
        )


# ---------------------------------------------------------------------------
# routes.py deactivate_repo() success-message job link
# ---------------------------------------------------------------------------


class TestDeactivateRepoSuccessMessageJobLinkUsesSearchParam:
    """
    The deactivation success-message job link must use search=.

    Exercises the REAL deactivate_repo route end-to-end (no mocking of the
    response-building code under test). Only genuine external boundaries
    are mocked: the admin-session store, the CSRF-token validator, and the
    ActivatedRepoManager (filesystem/DB layer) -- its
    list_activated_repositories/activated_repos_dir surface is left as an
    auto-mock so _get_all_activated_repos() safely falls back to an empty
    repo list via its existing try/except, without touching a real
    filesystem or database.
    """

    @pytest.fixture
    def app_client(self):
        import secrets as secrets_module
        from dataclasses import dataclass

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from code_indexer.server.web.auth import init_session_manager
        from code_indexer.server.web.routes import web_router

        # _create_repos_page_response() calls generate_csrf_token(), which
        # needs the global session manager initialized. Minimal fake config,
        # matching the established pattern in
        # test_session_refresh_integration_bug726.py.
        @dataclass
        class _FakeServerConfig:
            host: str = "127.0.0.1"

        @dataclass
        class _FakeWebSecurityConfig:
            web_session_timeout_seconds: int = 3600
            admin_session_timeout_seconds: int = 3600

        init_session_manager(
            secret_key=secrets_module.token_hex(32),
            config=_FakeServerConfig(),
            web_security_config=_FakeWebSecurityConfig(),
        )

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        mock_session = MagicMock()
        mock_session.username = "admin_user"
        mock_session.role = "admin"

        with patch(
            "code_indexer.server.web.routes._require_admin_session"
        ) as mock_auth:
            mock_auth.return_value = mock_session
            _bypass_elevation(app, web_router)
            yield TestClient(app)

    def test_job_link_uses_search_param_not_search_text(self, app_client):
        with patch(
            "code_indexer.server.web.routes.validate_login_csrf_token"
        ) as mock_csrf:
            mock_csrf.return_value = True

            with patch(
                "code_indexer.server.web.routes._get_activated_repo_manager"
            ) as mock_get_manager:
                mock_manager = MagicMock()
                mock_manager.deactivate_repository.return_value = "job-xyz-789"
                mock_get_manager.return_value = mock_manager

                response = app_client.post(
                    "/admin/repos/alice/myrepo/deactivate",
                    data={"csrf_token": "test-csrf-token"},
                )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:500]}"
        )
        mock_manager.deactivate_repository.assert_called_once()

        body = response.text
        assert "job-xyz-789" in body, (
            "Expected the job id to appear in the rendered repos page "
            f"response; got body head: {body[:500]}"
        )
        assert "search_text=job-xyz-789" not in body, (
            "deactivate_repo's success message job link must not use "
            "'?search_text=' -- the jobs_page route only binds 'search'"
        )
        assert "/admin/jobs?search=job-xyz-789" in body, (
            "Expected the rendered response to contain the job link "
            "'/admin/jobs?search=job-xyz-789'"
        )
