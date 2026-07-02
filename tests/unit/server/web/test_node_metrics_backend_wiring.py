"""
Unit tests verifying that node_metrics backend wiring is correct (Story #492 review fix).

Verifies:
1. routes.py dashboard-health reads from app.state.node_metrics_backend when set
   (i.e. it does NOT hard-code NodeMetricsSqliteBackend every request)
2. lifespan.py stores the node_metrics backend in app.state.node_metrics_backend
   so the route can access it without re-creating the backend

These are source-inspection tests because the wiring is boot-time logic
that cannot be exercised without the full app startup.
"""

from pathlib import Path


ROUTES_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/routes.py"
)

LIFESPAN_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/startup/lifespan.py"
)


class TestRoutesNodeMetricsBackendWiring:
    """routes.py must read node_metrics backend from app.state, not create its own."""

    def _routes_source(self) -> str:
        return ROUTES_PATH.read_text()

    def test_routes_reads_node_metrics_backend_from_app_state(self) -> None:
        """dashboard_health_partial must read node_metrics_backend from request.app.state."""
        src = self._routes_source()
        assert "node_metrics_backend" in src, (
            "routes.py must reference 'node_metrics_backend' from app.state "
            "instead of hard-coding NodeMetricsSqliteBackend"
        )

    def test_routes_uses_getattr_for_node_metrics_backend(self) -> None:
        """dashboard_health_partial must use getattr(request.app.state, 'node_metrics_backend', ...) pattern."""
        src = self._routes_source()
        # Must access node_metrics_backend from app state (via getattr or attribute access)
        assert (
            'getattr(request.app.state, "node_metrics_backend"' in src
            or "request.app.state.node_metrics_backend" in src
            or "getattr(request.app.state, 'node_metrics_backend'" in src
        ), (
            "dashboard_health_partial must access node_metrics_backend from request.app.state "
            "rather than instantiating a new SQLite backend on each request"
        )

    def test_routes_no_longer_unconditionally_creates_sqlite_backend(self) -> None:
        """dashboard_health_partial must not unconditionally create NodeMetricsSqliteBackend."""
        src = self._routes_source()
        # The old pattern was: _nm_backend = NodeMetricsSqliteBackend(_nm_db_path)
        # This must be gone (replaced by reading from app.state)
        assert "_nm_backend = NodeMetricsSqliteBackend(" not in src, (
            "dashboard_health_partial must not unconditionally create "
            "NodeMetricsSqliteBackend; use app.state.node_metrics_backend instead"
        )


class TestLifespanNodeMetricsBackendWiring:
    """lifespan.py must store the node_metrics backend in app.state.node_metrics_backend."""

    def _lifespan_source(self) -> str:
        return LIFESPAN_PATH.read_text()

    def test_lifespan_stores_node_metrics_backend_in_app_state(self) -> None:
        """lifespan.py must set app.state.node_metrics_backend to the backend instance."""
        src = self._lifespan_source()
        assert "app.state.node_metrics_backend" in src, (
            "lifespan.py must store the node_metrics backend as app.state.node_metrics_backend "
            "so routes.py can access it without re-creating the backend"
        )

    def test_lifespan_uses_backend_registry_node_metrics_when_available(self) -> None:
        """lifespan.py must use backend_registry.node_metrics when backend_registry is available."""
        src = self._lifespan_source()
        assert "backend_registry.node_metrics" in src, (
            "lifespan.py must use backend_registry.node_metrics when backend_registry is set "
            "(postgres mode), so NodeMetricsPostgresBackend is used instead of SQLite"
        )
