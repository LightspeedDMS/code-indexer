"""
Unit tests for Story #675: Stacked Bar Chart Card.

Tests the dashboard_service.get_chart_data() method, the new HTMX partial
route /admin/partials/dashboard-api-chart, the template rendering, and
the dashboard.html integration with Chart.js.

Following TDD methodology: tests written FIRST, then implementation.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Named constants for period seconds used throughout tests
PERIOD_1H = 3600
PERIOD_24H = 86400
PERIOD_7D = 604800

# Named constants for magic numbers
MIN_CHART_JS_SIZE_BYTES = 50_000  # Chart.js v4 minified is ~180KB; 50KB is a safe floor
REFRESH_FUNC_SCAN_BYTES = 2000  # Generous slice of refreshAll() body to search

# Repository root derived from test file location (avoids repeated path construction)
REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent

ROUTES_PATH = REPO_ROOT / "src/code_indexer/server/web/routes.py"
DASHBOARD_TEMPLATE_PATH = (
    REPO_ROOT / "src/code_indexer/server/web/templates/dashboard.html"
)
CHART_TEMPLATE_PATH = (
    REPO_ROOT
    / "src/code_indexer/server/web/templates/partials/dashboard_api_chart.html"
)
CHART_JS_PATH = REPO_ROOT / "src/code_indexer/server/web/static/js/chart.min.js"


@pytest.fixture()
def dashboard_service():
    """Provide a fresh DashboardService instance for each test."""
    from code_indexer.server.services.dashboard_service import DashboardService

    return DashboardService()


class TestGetChartDataAlwaysFourDatasets:
    """get_chart_data always returns exactly 4 datasets."""

    def test_get_chart_data_returns_4_datasets_with_data(self, dashboard_service):
        """Result always has exactly 4 dataset entries even with mixed data."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-01T10:00:00", "semantic", 5),
                ("2024-01-01T10:00:00", "regex", 2),
            ]
            result = dashboard_service.get_chart_data(PERIOD_24H)
        assert len(result["datasets"]) == 4

    def test_get_chart_data_returns_4_datasets_when_empty(self, dashboard_service):
        """Even with empty timeseries, result has 4 dataset entries."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = []
            result = dashboard_service.get_chart_data(PERIOD_24H)
        assert len(result["datasets"]) == 4

    def test_get_chart_data_dataset_labels_are_correct(self, dashboard_service):
        """Datasets have the correct human-readable labels."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = []
            result = dashboard_service.get_chart_data(PERIOD_24H)
        labels = [ds["label"] for ds in result["datasets"]]
        assert "Semantic" in labels
        assert "Other Index" in labels
        assert "Regex" in labels
        assert "Other Api" in labels

    def test_get_chart_data_datasets_have_background_color(self, dashboard_service):
        """Each dataset has a backgroundColor string starting with '#'."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = []
            result = dashboard_service.get_chart_data(PERIOD_24H)
        for ds in result["datasets"]:
            assert "backgroundColor" in ds
            assert isinstance(ds["backgroundColor"], str)
            assert ds["backgroundColor"].startswith("#")


class TestGetChartDataLabelCounts:
    """get_chart_data produces correct number of x-axis labels per period."""

    def _make_timeseries_for_7d(self):
        """Create 7 daily bucket entries (one per day) for a 7-day period."""
        days = [
            "2024-01-01T00:00:00",
            "2024-01-02T00:00:00",
            "2024-01-03T00:00:00",
            "2024-01-04T00:00:00",
            "2024-01-05T00:00:00",
            "2024-01-06T00:00:00",
            "2024-01-07T00:00:00",
        ]
        return [(d, "semantic", 1) for d in days]

    def _make_timeseries_for_24h(self):
        """Create 12 two-hour bucket entries for a 24h period."""
        hours = [
            "2024-01-01T00:00:00",
            "2024-01-01T02:00:00",
            "2024-01-01T04:00:00",
            "2024-01-01T06:00:00",
            "2024-01-01T08:00:00",
            "2024-01-01T10:00:00",
            "2024-01-01T12:00:00",
            "2024-01-01T14:00:00",
            "2024-01-01T16:00:00",
            "2024-01-01T18:00:00",
            "2024-01-01T20:00:00",
            "2024-01-01T22:00:00",
        ]
        return [(h, "semantic", 1) for h in hours]

    def test_get_chart_data_7d_returns_7_labels(self, dashboard_service):
        """7-day period with daily buckets produces 7 labels."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = (
                self._make_timeseries_for_7d()
            )
            result = dashboard_service.get_chart_data(PERIOD_7D)
        assert len(result["labels"]) == 7

    def test_get_chart_data_24h_returns_12_labels(self, dashboard_service):
        """24h period with 2-hour buckets produces 12 labels."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = (
                self._make_timeseries_for_24h()
            )
            result = dashboard_service.get_chart_data(PERIOD_24H)
        assert len(result["labels"]) == 12

    def test_get_chart_data_empty_timeseries_returns_empty_labels(
        self, dashboard_service
    ):
        """Empty timeseries produces empty labels list."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = []
            result = dashboard_service.get_chart_data(PERIOD_24H)
        assert result["labels"] == []

    def test_get_chart_data_dataset_data_length_matches_labels(self, dashboard_service):
        """Each dataset's data list length matches the number of labels."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = (
                self._make_timeseries_for_7d()
            )
            result = dashboard_service.get_chart_data(PERIOD_7D)
        num_labels = len(result["labels"])
        for ds in result["datasets"]:
            assert len(ds["data"]) == num_labels


class TestGetChartDataZeroFill:
    """get_chart_data fills missing metric types with zero rather than omitting them."""

    def test_missing_metric_types_filled_with_zero(self, dashboard_service):
        """Buckets that have no data for a metric type get zero, not missing."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            # Only semantic data at one bucket
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-01T10:00:00", "semantic", 5),
            ]
            result = dashboard_service.get_chart_data(PERIOD_24H)
        # Find the non-semantic datasets - they should all have [0]
        for ds in result["datasets"]:
            if ds["label"] != "Semantic":
                assert ds["data"] == [0], (
                    f"Dataset {ds['label']} should have [0] but got {ds['data']}"
                )

    def test_semantic_data_correct_count(self, dashboard_service):
        """Semantic dataset has the correct count in its bucket."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-01T10:00:00", "semantic", 7),
                ("2024-01-01T10:00:00", "regex", 3),
            ]
            result = dashboard_service.get_chart_data(PERIOD_24H)
        semantic_ds = next(ds for ds in result["datasets"] if ds["label"] == "Semantic")
        assert semantic_ds["data"] == [7]

    def test_multiple_metric_types_same_bucket(self, dashboard_service):
        """Multiple metric types in same bucket are mapped to correct datasets."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-01T00:00:00", "semantic", 10),
                ("2024-01-01T00:00:00", "other_index", 5),
                ("2024-01-01T00:00:00", "regex", 3),
                ("2024-01-01T00:00:00", "other_api", 1),
            ]
            result = dashboard_service.get_chart_data(PERIOD_24H)
        assert len(result["labels"]) == 1
        data_by_label = {ds["label"]: ds["data"][0] for ds in result["datasets"]}
        assert data_by_label["Semantic"] == 10
        assert data_by_label["Other Index"] == 5
        assert data_by_label["Regex"] == 3
        assert data_by_label["Other Api"] == 1

    def test_passes_period_seconds_to_timeseries(self, dashboard_service):
        """get_chart_data passes period_seconds to get_metrics_timeseries."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = []
            dashboard_service.get_chart_data(PERIOD_7D)
            mock_svc.get_metrics_timeseries.assert_called_once_with(PERIOD_7D)

    def test_uses_api_metrics_backend_when_provided(self, dashboard_service):
        """get_chart_data uses api_metrics_backend arg instead of singleton when given."""
        mock_backend = MagicMock()
        mock_backend.get_metrics_timeseries.return_value = []
        dashboard_service.get_chart_data(PERIOD_24H, api_metrics_backend=mock_backend)
        mock_backend.get_metrics_timeseries.assert_called_once_with(PERIOD_24H)

    def test_labels_sorted_chronologically(self, dashboard_service):
        """Labels appear in chronological order (ascending bucket_start)."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            # Deliberately out of order
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-03T00:00:00", "semantic", 3),
                ("2024-01-01T00:00:00", "semantic", 1),
                ("2024-01-02T00:00:00", "semantic", 2),
            ]
            result = dashboard_service.get_chart_data(PERIOD_7D)
        assert len(result["labels"]) == 3
        semantic_ds = next(ds for ds in result["datasets"] if ds["label"] == "Semantic")
        assert semantic_ds["data"] == [1, 2, 3]


class TestGetChartDataLabelFormatting:
    """get_chart_data formats x-axis labels appropriately per period."""

    def test_24h_period_labels_show_hour_format(self, dashboard_service):
        """24h period labels include hour information (e.g. '10:00' style)."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-01T10:00:00", "semantic", 1),
            ]
            result = dashboard_service.get_chart_data(PERIOD_24H)
        assert len(result["labels"]) == 1
        # Label should contain "10" (hour component)
        assert "10" in result["labels"][0]

    def test_7d_period_labels_show_date_format(self, dashboard_service):
        """7-day period labels include date information."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-15T00:00:00", "semantic", 1),
            ]
            result = dashboard_service.get_chart_data(PERIOD_7D)
        assert len(result["labels"]) == 1
        label = result["labels"][0]
        # Label should include day info - either "15" or month name
        assert "15" in label or "Jan" in label

    def test_1h_period_labels_show_minute_format(self, dashboard_service):
        """1-hour period labels include minute information."""
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_timeseries.return_value = [
                ("2024-01-01T10:05:00", "semantic", 1),
            ]
            result = dashboard_service.get_chart_data(PERIOD_1H)
        assert len(result["labels"]) == 1
        label = result["labels"][0]
        # 1h period uses 5-min buckets - label should show time like "10:05"
        assert "10" in label and "05" in label


class TestChartRouteInSource:
    """Source-inspection tests verifying routes.py registers the chart partial route."""

    def _routes_source(self) -> str:
        return ROUTES_PATH.read_text()

    def test_chart_route_function_defined(self):
        """routes.py must define dashboard_api_chart_partial function."""
        src = self._routes_source()
        assert "dashboard_api_chart_partial" in src, (
            "routes.py must define dashboard_api_chart_partial"
        )

    def test_chart_route_path_registered(self):
        """routes.py must register /partials/dashboard-api-chart."""
        src = self._routes_source()
        assert '"/partials/dashboard-api-chart"' in src, (
            "routes.py must register /partials/dashboard-api-chart"
        )

    def test_chart_route_function_signature_has_api_filter(self):
        """dashboard_api_chart_partial function signature must include api_filter param."""
        src = self._routes_source()
        # Locate the function definition and inspect its parameter list
        func_start = src.find("def dashboard_api_chart_partial(")
        assert func_start != -1, "routes.py must define dashboard_api_chart_partial"
        # Extract a window covering the function signature (up to closing paren)
        sig_window = src[func_start : func_start + 500]
        assert "api_filter" in sig_window, (
            "dashboard_api_chart_partial must declare api_filter in its signature"
        )

    def test_chart_route_calls_get_chart_data(self):
        """Route must call get_chart_data on dashboard_service."""
        src = self._routes_source()
        assert "get_chart_data" in src, (
            "routes.py must call dashboard_service.get_chart_data()"
        )

    def test_chart_route_uses_require_admin_session(self):
        """Route must check admin session (security requirement)."""
        src = self._routes_source()
        assert "_require_admin_session" in src

    def test_chart_route_renders_chart_template(self):
        """Route must render partials/dashboard_api_chart.html."""
        src = self._routes_source()
        assert "dashboard_api_chart.html" in src, (
            "routes.py must render partials/dashboard_api_chart.html"
        )


class TestChartTemplate:
    """Tests for partials/dashboard_api_chart.html template content."""

    def _template_source(self) -> str:
        return CHART_TEMPLATE_PATH.read_text()

    def test_chart_template_file_exists(self):
        """Template file must exist."""
        assert CHART_TEMPLATE_PATH.exists(), (
            "partials/dashboard_api_chart.html must exist"
        )

    def test_chart_template_has_canvas(self):
        """Template must contain a canvas element for Chart.js."""
        src = self._template_source()
        assert "<canvas" in src, "Template must have a <canvas> element"

    def test_chart_template_canvas_has_id(self):
        """Canvas must have id='apiUsageChart' for Chart.js to reference."""
        src = self._template_source()
        assert "apiUsageChart" in src, (
            "Canvas must have id='apiUsageChart' for Chart.js targeting"
        )

    def test_chart_template_has_destroy_pattern(self):
        """Template must implement destroy-before-recreate pattern."""
        src = self._template_source()
        assert "window._apiChart" in src, (
            "Template must use window._apiChart for destroy-before-recreate"
        )
        assert ".destroy()" in src, (
            "Template must call .destroy() to prevent canvas reuse errors"
        )

    def test_chart_template_destroy_runs_before_create(self):
        """Destroy check must appear before new Chart() creation."""
        src = self._template_source()
        destroy_pos = src.find(".destroy()")
        new_chart_pos = src.find("new Chart(")
        assert destroy_pos != -1, "Template must call .destroy()"
        assert new_chart_pos != -1, "Template must create new Chart()"
        assert destroy_pos < new_chart_pos, (
            ".destroy() must appear before new Chart() in the template"
        )

    def test_chart_template_has_chart_data_json(self):
        """Template must embed chart_data as JSON using tojson filter."""
        src = self._template_source()
        assert "chart_data" in src, "Template must reference chart_data variable"
        assert "tojson" in src, (
            "Template must use tojson filter to embed chart data as JSON"
        )

    def test_chart_template_has_stacked_config(self):
        """Template must configure stacked bars on both axes."""
        src = self._template_source()
        assert "stacked" in src, (
            "Template must configure stacked: true for bar chart axes"
        )

    def test_chart_template_bar_type(self):
        """Template must use 'bar' chart type."""
        src = self._template_source()
        assert "'bar'" in src or '"bar"' in src, (
            "Template must specify type: 'bar' for Chart.js"
        )

    def test_chart_template_is_card(self):
        """Template must use card styling consistent with other dashboard cards."""
        src = self._template_source()
        assert "card" in src, "Template must use card styling"

    def test_chart_template_has_iife(self):
        """Template script must use IIFE to avoid global scope pollution."""
        src = self._template_source()
        assert "(function()" in src or "(() =>" in src, (
            "Template script must use an IIFE to avoid global scope pollution"
        )


class TestChartJsAsset:
    """Tests verifying Chart.js static asset is present and valid."""

    def test_chartjs_file_exists(self):
        """chart.min.js must exist in the static/js directory."""
        assert CHART_JS_PATH.exists(), (
            f"chart.min.js must be present at {CHART_JS_PATH}"
        )

    def test_chartjs_file_is_not_empty(self):
        """chart.min.js must not be an empty placeholder (must be >{MIN_CHART_JS_SIZE_BYTES} bytes)."""
        assert CHART_JS_PATH.stat().st_size > MIN_CHART_JS_SIZE_BYTES, (
            f"chart.min.js must be the real Chart.js library "
            f"(>{MIN_CHART_JS_SIZE_BYTES} bytes), not an empty placeholder. "
            f"Actual size: {CHART_JS_PATH.stat().st_size} bytes"
        )

    def test_chartjs_file_contains_chart_constructor(self):
        """chart.min.js must contain Chart.js Chart constructor reference."""
        content = CHART_JS_PATH.read_text(encoding="utf-8", errors="replace")
        assert "Chart" in content, (
            "chart.min.js must contain the Chart constructor (Chart.js library)"
        )


class TestDashboardIntegration:
    """Tests for dashboard.html integration of the chart card."""

    def _dashboard_source(self) -> str:
        return DASHBOARD_TEMPLATE_PATH.read_text()

    def test_dashboard_loads_chartjs_script(self):
        """dashboard.html must include <script src=...chart.min.js...>."""
        src = self._dashboard_source()
        assert "chart.min.js" in src, (
            "dashboard.html must load chart.min.js via <script> tag"
        )

    def test_stats_template_has_chart_section(self):
        """dashboard_stats.html must contain the api-chart-section element."""
        stats_path = (
            DASHBOARD_TEMPLATE_PATH.parent / "partials" / "dashboard_stats.html"
        )
        src = stats_path.read_text()
        assert "api-chart-section" in src, (
            "dashboard_stats.html must contain api-chart-section"
        )

    def test_update_api_activity_fetches_chart_partial(self):
        """updateApiActivity() in dashboard_stats.html must fetch dashboard-api-chart."""
        stats_path = (
            DASHBOARD_TEMPLATE_PATH.parent / "partials" / "dashboard_stats.html"
        )
        src = stats_path.read_text()
        assert "dashboard-api-chart" in src, (
            "updateApiActivity() must include a fetch for dashboard-api-chart"
        )

    def test_chart_section_comes_after_per_user_section(self):
        """Chart card container must appear after the per-user section in dashboard_stats.html."""
        stats_path = (
            DASHBOARD_TEMPLATE_PATH.parent / "partials" / "dashboard_stats.html"
        )
        src = stats_path.read_text()
        per_user_pos = src.find("api-per-user-section")
        chart_pos = src.find("api-chart-section")
        assert per_user_pos != -1, "dashboard_stats.html must have api-per-user-section"
        assert chart_pos != -1, "dashboard_stats.html must have api-chart-section"
        assert chart_pos > per_user_pos, (
            "Chart card section must appear after the per-user section"
        )
