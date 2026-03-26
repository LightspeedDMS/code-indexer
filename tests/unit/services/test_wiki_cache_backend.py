import pytest


class TestWikiCacheBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import WikiCacheSqliteBackend

        b = WikiCacheSqliteBackend(str(tmp_path / "t.db"))
        yield b
        b.close()

    def test_satisfies_protocol(self, backend):
        from code_indexer.server.storage.protocols import WikiCacheBackend

        assert isinstance(backend, WikiCacheBackend)

    def test_registry_has_field(self):
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        assert "wiki_cache" in {f.name for f in dataclasses.fields(BackendRegistry)}
