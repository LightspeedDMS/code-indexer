"""Story #1293: BackendRegistry must have a search_embed_event field so
cluster mode uses the shared PostgreSQL backend, and lifespan.py must start
a SearchEmbedEventWriter (installed via set_search_embed_event_writer) so
emit_embed_event() actually persists rows in a real running server.

Mirrors test_lifespan_search_event_log_wiring_1159.py's established pattern
for Story #1159.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FACTORY_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "storage" / "factory.py"
)
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestBackendRegistryHasSearchEmbedEventField:
    def test_backend_registry_dataclass_has_search_embed_event_field(self):
        from code_indexer.server.storage.factory import BackendRegistry

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "search_embed_event" in field_names, (
            "BackendRegistry must have a search_embed_event field so "
            "lifespan.py can read backend_registry.search_embed_event for "
            "cluster-aware query-embedding decision event recording "
            "(Story #1293)"
        )


class TestSQLiteFactoryWiresSearchEmbedEvent:
    def test_sqlite_factory_wires_search_embed_event_backend(self, tmp_path):
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.services.search_embed_event_writer import (
            SearchEmbedEventSqliteBackend,
        )

        registry = StorageFactory._create_sqlite_backends(str(tmp_path))

        assert hasattr(registry, "search_embed_event"), (
            "_create_sqlite_backends() must populate search_embed_event"
        )
        assert isinstance(registry.search_embed_event, SearchEmbedEventSqliteBackend), (
            "search_embed_event in SQLite mode must be SearchEmbedEventSqliteBackend"
        )

    def test_factory_source_imports_search_embed_event_sqlite_backend(self):
        source = _FACTORY_PATH.read_text()
        assert "SearchEmbedEventSqliteBackend" in source, (
            "factory.py must import and use SearchEmbedEventSqliteBackend "
            "in _create_sqlite_backends()"
        )

    def test_factory_source_imports_search_embed_event_postgres_backend(self):
        source = _FACTORY_PATH.read_text()
        assert "SearchEmbedEventPostgresBackend" in source, (
            "factory.py must import and use SearchEmbedEventPostgresBackend "
            "in _create_postgres_backends()"
        )


class TestLifespanStartsSearchEmbedEventWriter:
    def test_lifespan_source_reads_backend_registry_search_embed_event(self):
        """lifespan.py must use backend_registry.search_embed_event directly
        (mirrors the search_event_log pattern) so cluster mode uses the
        shared PG backend."""
        source = _LIFESPAN_PATH.read_text()
        assert "backend_registry.search_embed_event" in source, (
            "lifespan.py must read backend_registry.search_embed_event "
            "directly so cluster mode uses the shared PG backend"
        )

    def test_lifespan_source_installs_writer_via_set_search_embed_event_writer(self):
        source = _LIFESPAN_PATH.read_text()
        assert "set_search_embed_event_writer" in source, (
            "lifespan.py must install the SearchEmbedEventWriter via "
            "set_search_embed_event_writer() so emit_embed_event() has a "
            "live writer to enqueue into during real server operation"
        )
