"""
Tests for Story #30 AC5: HTMX lazy-load health section.

Verifies dashboard.html uses HTMX lazy-loading for the health section
instead of synchronous server-side includes.
"""

from pathlib import Path

import pytest


class TestDashboardHTMXLazyLoad:
    """AC5: Tests for HTMX lazy-load health section."""

    @pytest.fixture
    def dashboard_template_content(self) -> str:
        """Load the dashboard.html template content."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / (
            "src/code_indexer/server/web/templates/dashboard.html"
        )
        return template_path.read_text()

    def test_health_section_uses_htmx_lazy_load(self, dashboard_template_content: str):
        """
        AC5: Health section should use hx-trigger="load" for lazy loading.

        Instead of {% include "partials/dashboard_health.html" %}, the
        health section should use HTMX to fetch content asynchronously.
        """
        # Should have hx-get for the health partial endpoint
        assert 'hx-get="/admin/partials/dashboard-health"' in dashboard_template_content

        # Should have hx-trigger="load" for lazy loading on page load
        assert 'hx-trigger="load"' in dashboard_template_content

    def test_health_section_has_loading_indicator(
        self, dashboard_template_content: str
    ):
        """AC5: Health section should show loading indicator while data loads."""
        # Should have some loading indicator text/element
        assert (
            "Loading" in dashboard_template_content
            or "loading" in dashboard_template_content
        )

    def test_health_section_does_not_use_synchronous_include(
        self, dashboard_template_content: str
    ):
        """AC5: Health section should NOT use synchronous server-side include."""
        # The health-section should not contain {% include "partials/dashboard_health.html" %}
        # Check that include is not inside the health-section
        # We need to find the health-section and verify it doesn't contain the include

        # Find the health section block
        health_start = dashboard_template_content.find('id="health-section"')
        assert health_start > 0, "health-section not found"

        # Find the closing tag for this section
        section_end = dashboard_template_content.find("</section>", health_start)
        assert section_end > health_start, "health-section closing tag not found"

        health_section_content = dashboard_template_content[health_start:section_end]

        # Should NOT contain synchronous include directive
        assert (
            '{% include "partials/dashboard_health.html" %}'
            not in health_section_content
        ), "AC5: Health section should use HTMX lazy-loading, not synchronous include"
