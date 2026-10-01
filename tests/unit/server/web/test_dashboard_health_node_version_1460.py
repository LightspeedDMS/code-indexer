"""Story #1460 AC1's operator-confirmation-procedure gap (Codex round-3
dual-review finding): the admin dashboard's node-metrics carousel
(`partials/dashboard_health.html`) never rendered each node's
`server_version`, even though it was already correctly persisted
(`NodeMetricsWriterService`) and retrieved (`get_latest_per_node()`, both
the SQLite and PostgreSQL backends) per node -- the field was silently
dropped between retrieval and template rendering.

This test renders the REAL Jinja2 template (via the actual `templates`
object `web/routes.py` constructs, with its real globals registered) with
a real multi-node `node_metrics` dataset -- proving the per-node version
now appears in the template's actual HTML output, scoped to the correct
node's own slide (not merely present anywhere on the page).
"""

from __future__ import annotations

from code_indexer.server.web.routes import templates


def _node_metrics_row(node_id: str, server_version: str) -> dict:
    """Mirrors the exact dict shape get_latest_per_node() returns (both
    the SQLite and PostgreSQL NodeMetrics backends), plus the "volumes"
    key routes.py's dashboard_health_partial adds by parsing volumes_json."""
    return {
        "node_id": node_id,
        "timestamp": "2026-07-25T00:00:00Z",
        "cpu_usage": 12.3,
        "memory_percent": 45.6,
        "process_rss_mb": 100.0,
        "index_memory_mb": 50.0,
        "swap_used_mb": 0.0,
        "swap_total_mb": 0.0,
        "disk_read_kb_s": 1.0,
        "disk_write_kb_s": 2.0,
        "net_rx_kb_s": 3.0,
        "net_tx_kb_s": 4.0,
        "volumes": [],
        "server_version": server_version,
    }


def _render_dashboard_health(node_metrics: list) -> str:
    template = templates.get_template("partials/dashboard_health.html")
    health = {
        "status": type("Status", (), {"value": "healthy"})(),
        "failure_reasons": [],
        "timestamp": None,
        "system": None,
    }
    rendered = template.render(
        {
            "health": health,
            "database_health": [],
            "server_version": "",
            "node_metrics": node_metrics,
            "scheduler_node_id": None,
            "dependency_rows": [],
        }
    )
    return str(rendered)


class TestDashboardHealthRendersPerNodeServerVersion:
    def test_each_node_slide_shows_its_own_server_version(self) -> None:
        rendered = _render_dashboard_health(
            [
                _node_metrics_row("node-1", "10.130.0"),
                _node_metrics_row("node-2", "10.129.5"),
            ]
        )

        # Both distinct per-node versions must appear -- not a single
        # global version, but each node's own reported value.
        assert "10.130.0" in rendered
        assert "10.129.5" in rendered

        # Scoped correctly: node-1's slide (nm-slide-0) contains its own
        # version, node-2's slide (nm-slide-1) contains its own, never
        # cross-contaminated.
        slide_0_start = rendered.index('id="nm-slide-0"')
        slide_1_start = rendered.index('id="nm-slide-1"')
        slide_0_html = rendered[slide_0_start:slide_1_start]
        slide_1_html = rendered[slide_1_start:]

        assert "10.130.0" in slide_0_html
        assert "10.129.5" not in slide_0_html
        assert "10.129.5" in slide_1_html
        assert "10.130.0" not in slide_1_html

    def test_single_node_slide_shows_server_version(self) -> None:
        rendered = _render_dashboard_health(
            [_node_metrics_row("node-solo", "10.128.3")]
        )

        assert "10.128.3" in rendered
