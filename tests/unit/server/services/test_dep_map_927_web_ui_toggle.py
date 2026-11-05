"""Story #927 Phase 3 AC10: Web UI toggle for dep_map_auto_repair_enabled.

The toggle is the cidx-depmap feature's setting: the config screen renders it
in the generic Feature Settings block from the feature's config section
(label and help text from its ``ConfigKey``), never in the core's Claude CLI
section.
"""

from pathlib import Path

from tests.unit.server._depmap_feature import render_depmap_feature_settings


class TestAC10WebUIToggle:
    """AC10: the dep_map_auto_repair_enabled toggle on the config screen."""

    def test_checkbox_present(self):
        assert (
            '<input type="checkbox" name="setting__dep_map_auto_repair_enabled"'
            in render_depmap_feature_settings()
        )

    def test_label_text_present(self):
        assert "Auto-repair after scheduled jobs" in render_depmap_feature_settings()

    def test_help_text_present(self):
        assert (
            "Repair once after a scheduled delta or refinement job that left "
            "anomalies" in render_depmap_feature_settings()
        )

    def test_not_in_the_core_claude_cli_section(self):
        template = (
            Path(__file__).parents[4]
            / "src/code_indexer/server/web/templates/partials/config_section.html"
        ).read_text()
        assert "dep_map_auto_repair_enabled" not in template
