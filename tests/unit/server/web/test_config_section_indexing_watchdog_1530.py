"""Template-rendering tests for the IndexingWatchdogConfig Web UI Config
section (Issue #1530), mirroring the Bug #1422 template-test pattern.
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
    start = html.find('id="section-indexing-watchdog"')
    assert start != -1, "Missing Indexing Watchdog <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


class TestTemplateRendersIndexingWatchdogSection:
    def test_display_mode_shows_value(self) -> None:
        section = _extract_section(_read_template())
        assert "config.indexing_watchdog.stale_activity_timeout_seconds" in section, (
            "Display table is missing the stale_activity_timeout_seconds value cell"
        )

    def test_edit_form_has_input(self) -> None:
        section = _extract_section(_read_template())
        assert 'name="stale_activity_timeout_seconds"' in section, (
            "Edit form is missing an <input> for stale_activity_timeout_seconds"
        )

    def test_edit_form_posts_to_correct_action(self) -> None:
        section = _extract_section(_read_template())
        assert 'action="/admin/config/indexing_watchdog"' in section
