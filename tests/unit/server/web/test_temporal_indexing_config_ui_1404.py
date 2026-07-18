"""Unit tests for Story #1404 - Web UI Config Screen exposure of the global
temporal indexing floor date (TemporalIndexingConfig.index_floor_date).

Mirrors the isolated <details>...</details> block extraction pattern used
for Story #1412's temporal_all_branches_enabled config UI test
(test_temporal_all_branches_gate_config_ui_1412.py) and the display/edit
mode structure established by search_timeouts / embedding_stats sections.
"""

from pathlib import Path

DETAILS_OPEN_TAG_LEN = len("<details")
DETAILS_CLOSE_TAG_LEN = len("</details>")


def _read_template() -> str:
    """Read config_section.html template content."""
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


def _read_temporal_indexing_section() -> str:
    """Return the isolated temporal-indexing <details>...</details> block."""
    html = _read_template()
    section_start = -1
    pos = html.find("<details")
    while pos != -1:
        tag_end = html.find(">", pos)
        if tag_end == -1:
            break
        if 'id="section-temporal-indexing"' in html[pos : tag_end + 1]:
            section_start = pos
            break
        pos = html.find("<details", pos + 1)
    assert section_start != -1, (
        "No <details element with id='section-temporal-indexing' found in "
        "config_section.html"
    )
    depth = 0
    i = section_start
    while i < len(html):
        if html[i : i + DETAILS_OPEN_TAG_LEN] == "<details":
            depth += 1
            i += DETAILS_OPEN_TAG_LEN
        elif html[i : i + DETAILS_CLOSE_TAG_LEN] == "</details>":
            depth -= 1
            if depth == 0:
                return html[section_start : i + DETAILS_CLOSE_TAG_LEN]
            i += DETAILS_CLOSE_TAG_LEN
        else:
            i += 1
    raise AssertionError("Unclosed <details id='section-temporal-indexing' block")


class TestTemporalIndexingConfigSectionExists:
    def test_section_present_in_template(self) -> None:
        # Will raise AssertionError from the helper if missing.
        section = _read_temporal_indexing_section()
        assert section  # non-empty


class TestTemporalIndexingConfigSectionDatePicker:
    def test_section_contains_date_input(self) -> None:
        section = _read_temporal_indexing_section()
        assert 'name="index_floor_date"' in section, (
            "config_section.html temporal indexing edit form must include "
            "an input named index_floor_date"
        )
        assert 'type="date"' in section, (
            "The floor-date field must be a validated calendar picker "
            "(<input type='date'>), per the story's explicit requirement"
        )

    def test_display_row_references_configured_value(self) -> None:
        section = _read_temporal_indexing_section()
        assert "config.temporal_indexing.index_floor_date" in section, (
            "Display mode must show the current floor date value"
        )

    def test_form_posts_to_correct_admin_config_endpoint(self) -> None:
        section = _read_temporal_indexing_section()
        assert 'action="/admin/config/temporal_indexing"' in section

    def test_validation_error_display_block_present(self) -> None:
        section = _read_temporal_indexing_section()
        assert "validation_errors.temporal_indexing" in section, (
            "Edit form must display a server-side validation error when "
            "present (Scenario 2: malformed dates rejected)"
        )


class TestTemporalIndexingConfigSectionAdvisoryNote:
    """Spec-corrections item 3: a non-blocking suggestion note about the
    consequence of lowering/clearing the floor date after some history has
    already been skipped (large unattended backfill on next incremental
    run)."""

    def test_advisory_note_about_backfill_on_lower_or_clear(self) -> None:
        section = _read_temporal_indexing_section()
        lowered = section.lower()
        assert "backfill" in lowered or "pre-floor" in lowered, (
            "Expected an advisory note mentioning the backfill consequence "
            "of lowering/clearing an already-applied floor date"
        )
