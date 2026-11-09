"""Tests for wiki refresh endpoint (Story #283)."""


class TestRefreshWikiEndpoint:
    def test_refresh_wiki_endpoint_exists(self):
        # The wiki's admin actions are the cidx-wiki feature's (phase 4e),
        # mounted under /admin like the core's web routes.
        from cidx_wiki.server.admin_routes import web_router

        paths = [r.path for r in web_router.routes]
        assert any("wiki-refresh" in p for p in paths)
