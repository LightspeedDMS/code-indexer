"""Root Cause 3 regression guard — Story #1159: BackendRegistry must have a
search_event_log field so cluster mode uses the shared PostgreSQL backend.

Before the fix, BackendRegistry had no search_event_log field, so
``hasattr(backend_registry, "search_event_log")`` was always False and
lifespan.py fell back to per-node SQLite even in PostgreSQL cluster mode.

These tests enforce:
  1. BackendRegistry dataclass declares search_event_log field.
  2. _create_sqlite_backends() wires SearchEventLogSqliteBackend into it.
  3. lifespan.py reads backend_registry.search_event_log directly (no hasattr).
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


class TestBackendRegistryHasSearchEventLogField:
    def test_backend_registry_dataclass_has_search_event_log_field(self):
        """BackendRegistry must declare search_event_log as a dataclass field."""
        from code_indexer.server.storage.factory import BackendRegistry

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "search_event_log" in field_names, (
            "BackendRegistry must have a search_event_log field so "
            "lifespan.py can read backend_registry.search_event_log for "
            "cluster-aware search event logging (Root Cause 3 / Story #1159)"
        )


class TestSQLiteFactoryWiresSearchEventLog:
    def test_sqlite_factory_wires_search_event_log_backend(self, tmp_path):
        """_create_sqlite_backends() must wire SearchEventLogSqliteBackend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        registry = StorageFactory._create_sqlite_backends(str(tmp_path))

        assert hasattr(registry, "search_event_log"), (
            "_create_sqlite_backends() must populate search_event_log"
        )
        assert isinstance(registry.search_event_log, SearchEventLogSqliteBackend), (
            "search_event_log in SQLite mode must be SearchEventLogSqliteBackend"
        )

    def test_factory_source_imports_search_event_log_sqlite_backend(self):
        """factory.py must import SearchEventLogSqliteBackend for SQLite wiring."""
        source = _FACTORY_PATH.read_text()
        assert "SearchEventLogSqliteBackend" in source, (
            "factory.py must import and use SearchEventLogSqliteBackend "
            "in _create_sqlite_backends()"
        )


class TestLifespanUsesBackendRegistrySearchEventLog:
    def test_lifespan_reads_backend_registry_search_event_log_directly(self):
        """lifespan.py must use backend_registry.search_event_log directly,
        not fall back to a per-node SQLite via hasattr check."""
        source = _LIFESPAN_PATH.read_text()
        assert "backend_registry.search_event_log" in source, (
            "lifespan.py must read backend_registry.search_event_log directly "
            "(not use hasattr fallback) so cluster mode uses the shared PG backend"
        )
