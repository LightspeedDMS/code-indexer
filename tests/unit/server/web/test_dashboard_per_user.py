"""
Unit tests for Story #674: Per-User API Usage Breakdown Card.

Tests the dashboard_service.get_per_user_stats() method, the new HTMX partial
route /admin/partials/dashboard-api-per-user, the template rendering, and
the dashboard.html integration.

Following TDD methodology: tests written FIRST, then implementation.
"""

from pathlib import Path

from unittest.mock import MagicMock, patch


# Named constants for period seconds used throughout tests
DAY_SECONDS = 86400
HOUR_SECONDS = 3600

ROUTES_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/routes.py"
)

DASHBOARD_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates/dashboard.html"
)

PER_USER_TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates/partials/dashboard_api_per_user.html"
)


class TestGetPerUserStats:
    """Tests for DashboardService.get_per_user_stats()."""

    def _make_service(self):
        from code_indexer.server.services.dashboard_service import DashboardService

        return DashboardService()

    def test_returns_empty_list_when_no_users(self):
        """Empty dict from api_metrics_service yields empty list."""
        service = self._make_service()
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_by_user.return_value = {}
            result = service.get_per_user_stats(DAY_SECONDS)
        assert result == []

    def test_single_user_all_metrics(self):
        """Single user row has correct fields and total."""
        service = self._make_service()
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_by_user.return_value = {
                "alice": {
                    "semantic": 10,
                    "other_index": 5,
                    "regex": 3,
                    "other_api": 2,
                }
            }
            result = service.get_per_user_stats(DAY_SECONDS)
        assert len(result) == 1
        row = result[0]
        assert row["username"] == "alice"
        assert row["semantic"] == 10
        assert row["other_index"] == 5
        assert row["regex"] == 3
        assert row["other_api"] == 2
        assert row["total"] == 20

    def test_sorted_by_total_descending(self):
        """Users are sorted highest total first."""
        service = self._make_service()
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_by_user.return_value = {
                "bob": {"semantic": 5, "other_index": 0, "regex": 0, "other_api": 0},
                "alice": {
                    "semantic": 30,
                    "other_index": 10,
                    "regex": 5,
                    "other_api": 5,
                },
                "carol": {
                    "semantic": 1,
                    "other_index": 0,
                    "regex": 0,
                    "other_api": 0,
                },
            }
            result = service.get_per_user_stats(DAY_SECONDS)
        assert len(result) == 3
        assert result[0]["username"] == "alice"
        assert result[0]["total"] == 50
        assert result[1]["username"] == "bob"
        assert result[1]["total"] == 5
        assert result[2]["username"] == "carol"
        assert result[2]["total"] == 1

    def test_passes_period_seconds_to_backend(self):
        """get_per_user_stats passes period_seconds to get_metrics_by_user."""
        service = self._make_service()
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            mock_svc.get_metrics_by_user.return_value = {}
            service.get_per_user_stats(HOUR_SECONDS)
            mock_svc.get_metrics_by_user.assert_called_once_with(HOUR_SECONDS)

    def test_missing_metric_keys_treated_as_zero(self):
        """If backend omits a metric key, it defaults to 0 in the row."""
        service = self._make_service()
        with patch(
            "code_indexer.server.services.dashboard_service.api_metrics_service"
        ) as mock_svc:
            # Only 'semantic' key provided
            mock_svc.get_metrics_by_user.return_value = {"dave": {"semantic": 7}}
            result = service.get_per_user_stats(DAY_SECONDS)
        assert len(result) == 1
        row = result[0]
        assert row["semantic"] == 7
        assert row["other_index"] == 0
        assert row["regex"] == 0
        assert row["other_api"] == 0
        assert row["total"] == 7

    def test_uses_api_metrics_backend_when_provided(self):
        """get_per_user_stats uses api_metrics_backend arg instead of singleton when given."""
        service = self._make_service()
        mock_backend = MagicMock()
        mock_backend.get_metrics_by_user.return_value = {
            "eve": {"semantic": 15, "other_index": 0, "regex": 0, "other_api": 0}
        }
        result = service.get_per_user_stats(
            DAY_SECONDS, api_metrics_backend=mock_backend
        )
        mock_backend.get_metrics_by_user.assert_called_once_with(DAY_SECONDS)
        assert len(result) == 1
        assert result[0]["username"] == "eve"
        assert result[0]["total"] == 15


class TestPerUserRouteInSource:
    """Source-inspection tests verifying that routes.py registers the path
    '/partials/dashboard-api-per-user'. The '/admin' prefix is added by the
    router mount prefix, not written in the path string itself.
    """

    def _routes_source(self) -> str:
        return ROUTES_PATH.read_text()

    def test_route_function_defined(self):
        """routes.py must define dashboard_api_per_user_partial function."""
        src = self._routes_source()
        assert "dashboard_api_per_user_partial" in src, (
            "routes.py must define dashboard_api_per_user_partial"
        )

    def test_route_path_registered(self):
        """routes.py must register /partials/dashboard-api-per-user (admin prefix added by router prefix)."""
        src = self._routes_source()
        assert '"/partials/dashboard-api-per-user"' in src, (
            "routes.py must register /partials/dashboard-api-per-user "
            "(the /admin prefix is added by the router prefix at mount time, "
            "not written in the path string)"
        )

    def test_route_accepts_api_filter_param(self):
        """Route must accept api_filter query parameter."""
        src = self._routes_source()
        assert "api_filter" in src

    def test_route_calls_get_per_user_stats(self):
        """Route must call get_per_user_stats on dashboard_service."""
        src = self._routes_source()
        assert "get_per_user_stats" in src, (
            "routes.py must call dashboard_service.get_per_user_stats()"
        )

    def test_route_uses_require_admin_session(self):
        """Route must check admin session (security requirement)."""
        src = self._routes_source()
        assert "_require_admin_session" in src

    def test_route_renders_per_user_template(self):
        """Route must render partials/dashboard_api_per_user.html."""
        src = self._routes_source()
        assert "dashboard_api_per_user.html" in src, (
            "routes.py must render partials/dashboard_api_per_user.html"
        )


class TestPerUserTemplate:
    """Tests for partials/dashboard_api_per_user.html template content."""

    def _template_source(self) -> str:
        return PER_USER_TEMPLATE_PATH.read_text()

    def test_template_file_exists(self):
        """Template file must exist."""
        assert PER_USER_TEMPLATE_PATH.exists(), (
            "partials/dashboard_api_per_user.html must exist"
        )

    def test_template_has_username_column(self):
        """Template must render Username column."""
        src = self._template_source()
        assert "Username" in src, "Template must have Username column header"

    def test_template_has_semantic_column(self):
        """Template must render Semantic column."""
        src = self._template_source()
        assert "Semantic" in src, "Template must have Semantic column header"

    def test_template_has_other_index_column(self):
        """Template must render Other Index column."""
        src = self._template_source()
        assert "Other Index" in src, "Template must have Other Index column header"

    def test_template_has_regex_column(self):
        """Template must render Regex column."""
        src = self._template_source()
        assert "Regex" in src, "Template must have Regex column header"

    def test_template_has_other_api_column(self):
        """Template must render Other API column."""
        src = self._template_source()
        assert "Other API" in src, "Template must have Other API column header"

    def test_template_has_total_column(self):
        """Template must render Total column."""
        src = self._template_source()
        assert "Total" in src, "Template must have Total column header"

    def test_template_has_empty_state(self):
        """Template must have an empty state message."""
        src = self._template_source()
        assert "No activity" in src, (
            "Template must show 'No activity' message when no users"
        )

    def test_template_iterates_per_user_data(self):
        """Template must iterate over per_user_data variable."""
        src = self._template_source()
        assert "per_user_data" in src, "Template must reference per_user_data variable"

    def test_template_renders_username(self):
        """Template must render username from each row."""
        src = self._template_source()
        assert "username" in src, "Template must render row.username"

    def test_template_renders_total(self):
        """Template must render row.total."""
        src = self._template_source()
        assert "total" in src, "Template must render row.total"

    def test_template_is_full_width_card(self):
        """Template must use col-12 for full width."""
        src = self._template_source()
        assert "col-12" in src, "Template must use col-12 for full-width layout"


class TestDashboardIntegration:
    """Tests for dashboard template integration of per-user card.

    Per-user section lives in dashboard_stats.html (included by dashboard.html).
    It is NOT in refreshAll() — it updates via updateApiActivity() on page load
    and when the period selector changes.
    """

    def _stats_source(self) -> str:
        stats_path = (
            DASHBOARD_TEMPLATE_PATH.parent / "partials" / "dashboard_stats.html"
        )
        return stats_path.read_text()

    def test_dashboard_has_per_user_section(self):
        """dashboard_stats.html must contain the api-per-user-section element."""
        src = self._stats_source()
        assert "api-per-user-section" in src, (
            "dashboard_stats.html must contain api-per-user-section"
        )

    def test_update_api_activity_fetches_per_user_partial(self):
        """updateApiActivity() must include a fetch of dashboard-api-per-user."""
        src = self._stats_source()
        assert "dashboard-api-per-user" in src, (
            "updateApiActivity() must include a fetch for dashboard-api-per-user"
        )
