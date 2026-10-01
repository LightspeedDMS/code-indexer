"""
Story #1676 AC3 Requirement 10: config_section.html's Telemetry section must
expose export_logs (display row + edit form) following the exact pattern of
its export_traces/export_metrics siblings, and be listed as a restart-
required field.

Mirrors the Jinja template structural coverage pattern established by
test_config_section_embedding_stats_1418.py.
"""

from pathlib import Path


def _read_template() -> str:
    template_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "partials"
        / "config_section.html"
    )
    return template_path.read_text()


def _extract_section(html: str) -> str:
    start = html.find('id="section-telemetry"')
    assert start != -1, "Missing Telemetry <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


class TestTelemetrySectionExposesExportLogs:
    def test_edit_form_has_export_logs_select_input(self) -> None:
        section = _extract_section(_read_template())
        assert '<select id="telemetry-export-logs" name="export_logs">' in section, (
            "Missing <select> edit-form input for export_logs"
        )

    def test_display_mode_shows_export_logs_row(self) -> None:
        section = _extract_section(_read_template())
        assert "config.telemetry.export_logs" in section

    def test_export_logs_marked_as_restart_required_in_template(self) -> None:
        section = _extract_section(_read_template())
        assert "'export_logs' in restart_required_fields" in section


class TestTelemetrySectionDescriptionMentionsLogs:
    def test_description_claims_traces_metrics_and_logs(self) -> None:
        section = _extract_section(_read_template())
        description_start = section.find("section-description")
        assert description_start != -1
        description_end = section.find("</p>", description_start)
        description = section[description_start:description_end].lower()
        assert "trac" in description
        assert "metric" in description
        assert "log" in description
