"""Regression guard for Issue #1160: QueryAnalyticsExportService lifespan wiring.

Mirrors test_lifespan_search_event_log_wiring_1159.py pattern.

Enforces:
  1. BackendRegistry declares query_analytics_exports field.
  2. _create_sqlite_backends() wires QueryAnalyticsExportSqliteBackend into it.
  3. lifespan.py reads backend_registry.query_analytics_exports (not hasattr).
  4. lifespan.py sets app.state.query_analytics_export_service after startup.
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


class TestBackendRegistryHasQueryAnalyticsExportsField:
    def test_backend_registry_dataclass_has_query_analytics_exports_field(self):
        """BackendRegistry must declare query_analytics_exports as a dataclass field."""
        from code_indexer.server.storage.factory import BackendRegistry

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "query_analytics_exports" in field_names, (
            "BackendRegistry must have a query_analytics_exports field so "
            "lifespan.py can read backend_registry.query_analytics_exports for "
            "cluster-aware export tracking (Issue #1160)"
        )


class TestSQLiteFactoryWiresQueryAnalyticsExports:
    def test_sqlite_factory_wires_query_analytics_exports_backend(self, tmp_path):
        """_create_sqlite_backends() must wire QueryAnalyticsExportSqliteBackend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.services.query_analytics_export_service import (
            QueryAnalyticsExportSqliteBackend,
        )

        registry = StorageFactory._create_sqlite_backends(str(tmp_path))

        assert hasattr(registry, "query_analytics_exports"), (
            "_create_sqlite_backends() must populate query_analytics_exports"
        )
        assert isinstance(
            registry.query_analytics_exports, QueryAnalyticsExportSqliteBackend
        ), (
            "query_analytics_exports in SQLite mode must be QueryAnalyticsExportSqliteBackend"
        )

    def test_factory_source_imports_query_analytics_export_sqlite_backend(self):
        """factory.py must import QueryAnalyticsExportSqliteBackend for SQLite wiring."""
        source = _FACTORY_PATH.read_text()
        assert "QueryAnalyticsExportSqliteBackend" in source, (
            "factory.py must import and use QueryAnalyticsExportSqliteBackend "
            "in _create_sqlite_backends()"
        )


class TestLifespanSetsQueryAnalyticsExportService:
    def test_lifespan_source_sets_query_analytics_export_service_on_app_state(self):
        """lifespan.py must assign app.state.query_analytics_export_service."""
        source = _LIFESPAN_PATH.read_text()
        assert "app.state.query_analytics_export_service" in source, (
            "lifespan.py must set app.state.query_analytics_export_service so "
            "the export endpoints can read it (Issue #1160)"
        )

    def test_lifespan_source_imports_query_analytics_export_service(self):
        """lifespan.py must import QueryAnalyticsExportService."""
        source = _LIFESPAN_PATH.read_text()
        assert "QueryAnalyticsExportService" in source, (
            "lifespan.py must import QueryAnalyticsExportService (Issue #1160)"
        )

    def test_lifespan_source_reads_backend_registry_query_analytics_exports(self):
        """lifespan.py must use backend_registry.query_analytics_exports directly."""
        source = _LIFESPAN_PATH.read_text()
        assert "backend_registry.query_analytics_exports" in source, (
            "lifespan.py must read backend_registry.query_analytics_exports directly "
            "so cluster mode uses the shared PG backend (Issue #1160)"
        )
