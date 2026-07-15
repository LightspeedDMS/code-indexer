"""Unit tests for Story #1412 - Web UI Config Screen exposure of
IndexingConfig.temporal_all_branches_enabled.

Per the "No Environment Variables for Server Settings" invariant, this gate
MUST be operator-toggleable via the Web Config screen (not backend-only).
Mirrors the static-content <details> block isolation pattern used for
Bug #943's totp_elevation config section
(tests/unit/server/web/test_totp_elevation_config_section_943.py).
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


def _read_indexing_section() -> str:
    """Return the isolated indexing <details>...</details> block."""
    html = _read_template()
    section_start = -1
    pos = html.find("<details")
    while pos != -1:
        tag_end = html.find(">", pos)
        if tag_end == -1:
            break
        if 'id="section-indexing"' in html[pos : tag_end + 1]:
            section_start = pos
            break
        pos = html.find("<details", pos + 1)
    assert section_start != -1, (
        "No <details element with id='section-indexing' found in config_section.html"
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
    raise AssertionError("Unclosed <details id='section-indexing' block")


class TestIndexingConfigSectionTemporalAllBranchesCheckbox:
    """The indexing edit form must expose a checkbox for the gate."""

    def test_section_contains_checkbox_input(self) -> None:
        section = _read_indexing_section()
        assert 'name="temporal_all_branches_enabled"' in section, (
            "config_section.html indexing edit form must include an input "
            "named temporal_all_branches_enabled"
        )
        assert 'type="checkbox"' in section

    def test_checkbox_has_preceding_hidden_fallback(self) -> None:
        """
        Unchecking a bare checkbox omits the key from form POST data entirely
        (browser behavior), which would make the gate impossible to turn OFF
        via the Web UI. A hidden fallback input with the same name, placed
        BEFORE the checkbox in DOM order, guarantees the key is always
        submitted (last value wins) -- same pattern as Bug #943's
        elevation_enforcement_enabled field.
        """
        section = _read_indexing_section()
        checkbox_pos = section.find(
            'type="checkbox" id="indexing-temporal-all-branches-enabled"'
        )
        assert checkbox_pos != -1, (
            "Expected checkbox with id='indexing-temporal-all-branches-enabled'"
        )
        hidden_marker = (
            'type="hidden" name="temporal_all_branches_enabled" value="false"'
        )
        hidden_pos = section.find(hidden_marker)
        assert hidden_pos != -1, (
            f"Expected a hidden fallback input ({hidden_marker!r}) before the checkbox"
        )
        assert hidden_pos < checkbox_pos, (
            "Hidden fallback input must appear BEFORE the checkbox in DOM order "
            "so the checked checkbox's 'true' value wins when checked."
        )

    def test_display_row_references_gate_value(self) -> None:
        section = _read_indexing_section()
        assert "config.indexing.temporal_all_branches_enabled" in section, (
            "Display mode must show the current gate value"
        )
