"""Tests for wiki toggle endpoint (Story #280)."""


class TestWikiToggleEndpoint:
    def test_toggle_endpoint_exists_in_web_routes(self):
        from code_indexer.server.web.routes import web_router
        paths = [r.path for r in web_router.routes]
        assert any("wiki-toggle" in p for p in paths)
