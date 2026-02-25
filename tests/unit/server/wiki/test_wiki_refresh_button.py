"""Tests for wiki refresh endpoint (Story #283)."""


class TestRefreshWikiEndpoint:
    def test_refresh_wiki_endpoint_exists(self):
        from code_indexer.server.web.routes import web_router
        paths = [r.path for r in web_router.routes]
        assert any("wiki-refresh" in p for p in paths)
