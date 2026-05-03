"""Story #927 Phase 3 AC10: Web UI toggle for dep_map_auto_repair_enabled.

Structural content tests of the HTML template source.

AC10: config_section.html must contain:
  - An exact <select fragment for dep_map_auto_repair_enabled
  - Label text "Auto-repair after scheduled jobs"
  - Exact distinctive help text from the story spec
"""

from pathlib import Path


def _template_text() -> str:
    template_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "partials"
        / "config_section.html"
    )
    return template_path.read_text()


class TestAC10WebUIToggle:
    """AC10: dep_map_auto_repair_enabled toggle present in claude-integration config section."""

    def test_select_element_present(self):
        """Direct substring confirms <select with name='dep_map_auto_repair_enabled' is present."""
        assert (
            '<select id="dep-map-auto-repair-enabled" name="dep_map_auto_repair_enabled"'
            in _template_text()
        )

    def test_label_text_present(self):
        """config_section.html contains 'Auto-repair after scheduled jobs' label text."""
        assert "Auto-repair after scheduled jobs" in _template_text()

    def test_help_text_present(self):
        """config_section.html contains the exact help text for the auto-repair toggle."""
        assert (
            "When enabled, scheduled delta and refinement jobs automatically run a repair"
            " pass once if anomalies are detected." in _template_text()
        )
