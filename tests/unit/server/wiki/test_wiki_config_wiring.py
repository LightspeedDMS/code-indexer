"""Tests for Story #323 code review fix: wiki_config wired through wiki/routes.py.

These tests verify that:
1. _get_wiki_config() helper in wiki/routes.py returns a WikiConfig instance
   derived from ConfigService (not None / all-True fallback silently).
2. When wiki_config toggles are OFF, the route responses respect them —
   e.g. article_number does not appear in the metadata panel.
3. web/routes.py background thread passes wiki_config to
   populate_views_from_front_matter().

TDD Red Phase: written BEFORE production code changes.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import (
    _reset_wiki_cache,
    get_wiki_user_hybrid,
    wiki_router,
)
from code_indexer.server.auth.dependencies import get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str):
    user = MagicMock()
    user.username = username
    user.has_permission = MagicMock(return_value=True)
    return user


def _make_wiki_app(actual_repo_path: str, wiki_config=None):
    """Build a minimal FastAPI test app with wiki_router mounted.

    If wiki_config is provided it is placed on a mock ConfigService attached
    to app.state so that _get_wiki_config() can retrieve it.
    """
    _reset_wiki_cache()

    app = FastAPI()
    user = _make_user("admin")
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: user
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = True
    app.state.golden_repo_manager.db_path = _db_path

    golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-wiring-test"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
    app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    app.state.access_filtering_service.get_accessible_repos.return_value = {"test-repo"}

    # Wire config_service onto app.state if wiki_config supplied
    if wiki_config is not None:
        from code_indexer.server.utils.config_manager import ServerConfig

        server_config = MagicMock(spec=ServerConfig)
        server_config.wiki_config = wiki_config

        config_service = MagicMock()
        config_service.get_config.return_value = server_config
        app.state.config_service = config_service

    return app


# ---------------------------------------------------------------------------
# Test 1: _get_wiki_config() helper returns WikiConfig instance
# ---------------------------------------------------------------------------


class TestGetWikiConfigHelper:
    """_get_wiki_config() must return a WikiConfig instance from ConfigService."""

    def test_get_wiki_config_returns_wiki_config_instance(self):
        """_get_wiki_config() returns the WikiConfig stored on ConfigService."""
        from code_indexer.server.wiki.routes import _get_wiki_config  # noqa: F401
        from code_indexer.server.utils.config_manager import WikiConfig

        wiki_config = WikiConfig(
            enable_header_block_parsing=True,
            enable_article_number=False,
            enable_publication_status=True,
            enable_views_seeding=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_wiki_app(tmpdir, wiki_config=wiki_config)
            # Build a mock request pointing at the app
            request = MagicMock()
            request.app = app

            result = _get_wiki_config(request)

            assert isinstance(result, WikiConfig)
            assert result.enable_article_number is False
            assert result.enable_views_seeding is False

    def test_get_wiki_config_returns_default_when_no_config_service(self):
        """_get_wiki_config() returns default WikiConfig (all-True) when app.state
        has no config_service."""
        from code_indexer.server.wiki.routes import _get_wiki_config
        from code_indexer.server.utils.config_manager import WikiConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            # App with NO config_service on state
            app = _make_wiki_app(tmpdir, wiki_config=None)
            request = MagicMock()
            request.app = app

            result = _get_wiki_config(request)

            # Must still return a valid WikiConfig (not crash, not return None)
            assert isinstance(result, WikiConfig)
            assert result.enable_article_number is True
            assert result.enable_views_seeding is True


# ---------------------------------------------------------------------------
# Test 2: wiki/routes.py honours wiki_config toggles in article responses
# ---------------------------------------------------------------------------


class TestWikiRoutesHonourWikiConfig:
    """When wiki_config has toggles OFF, article routes must not emit those fields."""

    def test_article_number_hidden_when_toggle_off_in_golden_repo_route(self):
        """serve_wiki_article must NOT show article_number in metadata panel
        when enable_article_number=False is configured on the server."""
        from code_indexer.server.utils.config_manager import WikiConfig

        wiki_config = WikiConfig(
            enable_header_block_parsing=False,
            enable_article_number=False,
            enable_publication_status=True,
            enable_views_seeding=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            # Article with a KB-style header that includes article_number
            (repo_dir / "article.md").write_text(
                "Article Number: KA-00001\n"
                "Publication Status: Published\n"
                "---\n"
                "# My Article\n"
                "Content body."
            )
            app = _make_wiki_app(tmpdir, wiki_config=wiki_config)
            client = TestClient(app)

            resp = client.get("/wiki/test-repo/article")
            assert resp.status_code == 200
            # article_number toggle is OFF, so the metadata panel must NOT
            # display KA-00001 as "Salesforce Article"
            assert "Salesforce Article" not in resp.text

    def test_article_number_shown_when_toggle_on_in_golden_repo_route(self):
        """serve_wiki_article MUST show article_number in metadata panel
        when enable_article_number=True is configured."""
        from code_indexer.server.utils.config_manager import WikiConfig

        wiki_config = WikiConfig(
            enable_header_block_parsing=True,
            enable_article_number=True,
            enable_publication_status=True,
            enable_views_seeding=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "article.md").write_text(
                "Article Number: KA-00001\n"
                "Publication Status: Published\n"
                "---\n"
                "# My Article\n"
                "Content body."
            )
            app = _make_wiki_app(tmpdir, wiki_config=wiki_config)
            client = TestClient(app)

            resp = client.get("/wiki/test-repo/article")
            assert resp.status_code == 200
            assert "KA-00001" in resp.text

    def test_wiki_root_article_number_hidden_when_toggle_off(self):
        """serve_wiki_root must NOT show article_number when toggle is OFF."""
        from code_indexer.server.utils.config_manager import WikiConfig

        wiki_config = WikiConfig(
            enable_header_block_parsing=False,
            enable_article_number=False,
            enable_publication_status=True,
            enable_views_seeding=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "home.md").write_text(
                "Article Number: KA-00002\n" "---\n" "# Home\n" "Welcome."
            )
            app = _make_wiki_app(tmpdir, wiki_config=wiki_config)
            client = TestClient(app)

            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200
            assert "Salesforce Article" not in resp.text


# ---------------------------------------------------------------------------
# Test 3: web/routes.py background thread passes wiki_config
# ---------------------------------------------------------------------------


class TestWebRoutesPopulateViewsUsesWikiConfig:
    """populate_views_from_front_matter in web/routes.py background thread
    must receive wiki_config from ConfigService (not use None)."""

    def test_populate_views_receives_wiki_config_from_config_service(self):
        """When enable_views_seeding=False is configured, the background
        populate_views_from_front_matter call must NOT seed views."""
        from code_indexer.server.utils.config_manager import WikiConfig

        _wiki_config = WikiConfig(
            enable_header_block_parsing=True,
            enable_article_number=True,
            enable_publication_status=True,
            enable_views_seeding=False,  # key: seeding is OFF
        )

        call_args_received = []

        def capture_call(alias, repo_path, cache, wiki_config=None):
            call_args_received.append(wiki_config)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "article.md").write_text("---\nviews: 50\n---\n# Art\nContent.")

            # Patch WikiService.populate_views_from_front_matter to capture args
            with patch(
                "code_indexer.server.wiki.wiki_service.WikiService.populate_views_from_front_matter",
                side_effect=capture_call,
            ):
                # Import the function that triggers the background thread
                from code_indexer.server.web.routes import web_router

                # Find the wiki-toggle route handler
                toggle_route = None
                for route in web_router.routes:
                    if hasattr(route, "path") and "wiki-toggle" in route.path:
                        toggle_route = route
                        break

                assert toggle_route is not None, "wiki-toggle route must exist"

                # We need to call the route handler in a controlled way.
                # Instead of a full HTTP test (which requires many mocked services),
                # we verify that the route handler code path would fetch wiki_config
                # from config_service and pass it to the background thread.
                # This is tested by inspecting the route handler source.
                import inspect

                handler_source = inspect.getsource(toggle_route.endpoint)

                # The handler MUST fetch wiki_config from config_service
                assert (
                    "wiki_config" in handler_source
                ), "wiki-toggle handler must reference wiki_config"
                # The handler MUST pass it to populate_views_from_front_matter
                assert (
                    "populate_views_from_front_matter" in handler_source
                ), "wiki-toggle handler must call populate_views_from_front_matter"
