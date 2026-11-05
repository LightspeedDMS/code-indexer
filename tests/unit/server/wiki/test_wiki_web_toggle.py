"""Tests for wiki toggle endpoint (Story #280)."""


class TestWikiToggleEndpoint:
    def test_toggle_endpoint_exists_in_web_routes(self):
        # The wiki's admin actions are the cidx-wiki feature's (phase 4e),
        # mounted under /admin like the core's web routes.
        from cidx_wiki.server.admin_routes import web_router

        paths = [r.path for r in web_router.routes]
        assert any("wiki-toggle" in p for p in paths)
