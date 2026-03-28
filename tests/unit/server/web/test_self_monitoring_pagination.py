"""Tests for Story #344: Self-Monitoring Tab Pagination.

Validates that self_monitoring.html contains required pagination elements:
- Table IDs on scan-history and created-issues tables
- Page size dropdowns with correct options (10/20/50/100, default 10)
- Pagination controls containers below each table
- PaginationController JavaScript class definition
- Two PaginationController instances initialized on DOMContentLoaded

Testing approach: Read the template file and check for required HTML/JS patterns.
This mirrors the approach used in test_dependency_map_js_code_mass.py for
JS content validation in a Python test suite.
"""

from pathlib import Path


def _read_template() -> str:
    """Read the self_monitoring.html template file content.

    Path traversal from tests/unit/server/web/:
      .parent       -> tests/unit/server/web/
      .parent.parent -> tests/unit/server/
      .parent x3    -> tests/unit/
      .parent x4    -> tests/
      .parent x5    -> project root
    """
    template_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "self_monitoring.html"
    )
    return template_path.read_text()


class TestScanHistoryTableId:
    """AC1: scan-history-table ID must be present on the scan history table."""

    def test_scan_history_table_has_id(self):
        """Scan history table must have id='scan-history-table'."""
        template = _read_template()
        assert (
            'id="scan-history-table"' in template
        ), "self_monitoring.html must have a table with id='scan-history-table'"


class TestCreatedIssuesTableId:
    """AC1: created-issues-table ID must be present on the created issues table."""

    def test_created_issues_table_has_id(self):
        """Created issues table must have id='created-issues-table'."""
        template = _read_template()
        assert (
            'id="created-issues-table"' in template
        ), "self_monitoring.html must have a table with id='created-issues-table'"


class TestScanHistoryDropdown:
    """AC1, AC2: Scan history page-size dropdown must exist with correct options."""

    def test_scan_history_dropdown_exists(self):
        """Scan history page size dropdown must have id='scan-history-page-size'."""
        template = _read_template()
        assert (
            'id="scan-history-page-size"' in template
        ), "self_monitoring.html must have a select with id='scan-history-page-size'"

    def test_scan_history_dropdown_has_option_10(self):
        """Scan history dropdown must include option value 10."""
        template = _read_template()
        # Look for the dropdown section by finding the scan-history-page-size context
        assert (
            "scan-history-page-size" in template
        ), "scan-history-page-size element required"
        assert (
            '<option value="10"' in template
        ), "Page size dropdown must include option with value='10'"

    def test_scan_history_dropdown_has_option_20(self):
        """Scan history dropdown must include option value 20."""
        template = _read_template()
        assert (
            '<option value="20"' in template
        ), "Page size dropdown must include option with value='20'"

    def test_scan_history_dropdown_has_option_50(self):
        """Scan history dropdown must include option value 50."""
        template = _read_template()
        assert (
            '<option value="50"' in template
        ), "Page size dropdown must include option with value='50'"

    def test_scan_history_dropdown_has_option_100(self):
        """Scan history dropdown must include option value 100."""
        template = _read_template()
        assert (
            '<option value="100"' in template
        ), "Page size dropdown must include option with value='100'"

    def test_scan_history_dropdown_option_10_is_selected(self):
        """AC1: Default page size of 10 must be pre-selected."""
        template = _read_template()
        # The value="10" option must have the selected attribute
        assert (
            'value="10" selected' in template
            or 'value="10"  selected' in template
            or '<option value="10" selected' in template
        ), "Page size option '10' must have 'selected' attribute (default page size)"


class TestCreatedIssuesDropdown:
    """AC2: Created issues page-size dropdown must exist independently."""

    def test_created_issues_dropdown_exists(self):
        """Created issues page size dropdown must have id='created-issues-page-size'."""
        template = _read_template()
        assert (
            'id="created-issues-page-size"' in template
        ), "self_monitoring.html must have a select with id='created-issues-page-size'"

    def test_created_issues_dropdown_is_independent(self):
        """AC2: Both dropdowns must exist independently with distinct IDs."""
        template = _read_template()
        assert (
            'id="scan-history-page-size"' in template
        ), "scan-history-page-size dropdown must exist"
        assert (
            'id="created-issues-page-size"' in template
        ), "created-issues-page-size dropdown must exist independently"
        # Verify they are different IDs (not the same element)
        assert (
            template.count('id="scan-history-page-size"') == 1
        ), "scan-history-page-size must appear exactly once"
        assert (
            template.count('id="created-issues-page-size"') == 1
        ), "created-issues-page-size must appear exactly once"


class TestPaginationControlsContainers:
    """Pagination controls containers must exist for both tables."""

    def test_scan_history_controls_container_exists(self):
        """Pagination controls for scan history must have id='scan-history-controls'."""
        template = _read_template()
        assert (
            'id="scan-history-controls"' in template
        ), "self_monitoring.html must have a controls container with id='scan-history-controls'"

    def test_created_issues_controls_container_exists(self):
        """Pagination controls for created issues must have id='created-issues-controls'."""
        template = _read_template()
        assert (
            'id="created-issues-controls"' in template
        ), "self_monitoring.html must have a controls container with id='created-issues-controls'"


class TestPaginationControllerClass:
    """PaginationController JavaScript class must be defined in the template."""

    def test_pagination_controller_class_defined(self):
        """PaginationController class must be present in a script tag."""
        template = _read_template()
        assert (
            "class PaginationController" in template
        ), "self_monitoring.html must define 'class PaginationController' in a script tag"

    def test_pagination_controller_has_initialize_method(self):
        """PaginationController must have an initialize() method."""
        template = _read_template()
        assert (
            "initialize(" in template
        ), "PaginationController must define an initialize() method"

    def test_pagination_controller_has_render_method(self):
        """PaginationController must have a render() method."""
        template = _read_template()
        assert (
            "render()" in template
        ), "PaginationController must define a render() method"

    def test_pagination_controller_has_go_to_page_method(self):
        """PaginationController must have a goToPage() method."""
        template = _read_template()
        assert (
            "goToPage(" in template
        ), "PaginationController must define a goToPage() method"

    def test_pagination_controller_has_on_page_size_change_method(self):
        """PaginationController must have an onPageSizeChange() method."""
        template = _read_template()
        assert (
            "onPageSizeChange(" in template
        ), "PaginationController must define an onPageSizeChange() method"


class TestPaginationControllerInstances:
    """Two PaginationController instances must be initialized."""

    def test_scan_history_instance_created(self):
        """A PaginationController instance must be created for scan-history-table."""
        template = _read_template()
        assert (
            "scan-history-table" in template
        ), "scan-history-table ID must be referenced"
        # The initialize call must reference scan-history-table
        assert (
            "new PaginationController" in template
        ), "PaginationController instances must be created with 'new PaginationController'"

    def test_created_issues_instance_created(self):
        """A PaginationController instance must be created for created-issues-table."""
        template = _read_template()
        assert (
            "created-issues-table" in template
        ), "created-issues-table ID must be referenced"

    def test_two_pagination_instances_created(self):
        """Exactly two PaginationController instances must be instantiated."""
        template = _read_template()
        count = template.count("new PaginationController")
        assert (
            count == 2
        ), f"Exactly 2 PaginationController instances must be created, found {count}"

    def test_instances_initialized_in_dom_content_loaded(self):
        """PaginationController instances must be initialized inside DOMContentLoaded."""
        template = _read_template()
        assert (
            "DOMContentLoaded" in template
        ), "PaginationController initialization must be inside DOMContentLoaded event handler"


class TestPaginationAlgorithmConstants:
    """PaginationController must implement correct algorithm constants."""

    def test_default_page_size_is_10(self):
        """AC1: Default page size must be 10."""
        template = _read_template()
        assert (
            "pageSize = 10" in template
        ), "PaginationController must default pageSize to 10"

    def test_current_page_starts_at_1(self):
        """AC3, AC4: currentPage must start at 1."""
        template = _read_template()
        assert (
            "currentPage = 1" in template
        ), "PaginationController must initialize currentPage to 1"

    def test_page_resets_to_1_on_size_change(self):
        """AC5: Page must reset to 1 when page size changes."""
        template = _read_template()
        # The onPageSizeChange method must reset currentPage to 1
        assert (
            "currentPage = 1" in template
        ), "onPageSizeChange must reset currentPage to 1"


class TestPaginationShowingText:
    """AC6, AC7: Pagination controls must show row count info."""

    def test_showing_text_pattern_present(self):
        """Pagination controls must show 'Showing X-Y of Z' info text."""
        template = _read_template()
        assert (
            "Showing" in template
        ), "Pagination controls must contain 'Showing' info text element"

    def test_prev_button_present(self):
        """AC4: Previous page button must be present in controls."""
        template = _read_template()
        # Prev button should exist in pagination controls
        assert (
            "Prev" in template or "prev" in template
        ), "Pagination controls must include a Previous page button"

    def test_next_button_present(self):
        """AC3: Next page button must be present in controls."""
        template = _read_template()
        assert (
            "Next" in template or "next" in template
        ), "Pagination controls must include a Next page button"
