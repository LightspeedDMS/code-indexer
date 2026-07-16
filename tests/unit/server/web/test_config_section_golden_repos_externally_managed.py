"""Unit tests for Web UI Config Screen exposure of
GoldenReposConfig.externally_managed (EVO-64493 code-review follow-up).

Per the "No Environment Variables for Server Settings" invariant, this flag
MUST be operator-toggleable via the Web Config screen (not backend-only).
Mirrors the hidden-fallback-checkbox pattern used for Story #1412's
temporal_all_branches_enabled field
(tests/unit/server/web/test_temporal_all_branches_gate_config_ui_1412.py).
"""

from pathlib import Path

from code_indexer.server.web.routes import _validate_config_section


def _read_template() -> str:
    """Read config_section.html template content."""
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


def _read_golden_repos_section() -> str:
    """Return the isolated golden_repos <details>...</details> block."""
    html = _read_template()
    marker = 'id="section-golden_repos"'
    marker_pos = html.find(marker)
    assert marker_pos != -1, "Missing Golden Repository Settings <details> section"
    section_start = html.rfind("<details", 0, marker_pos)
    assert section_start != -1
    end = html.find("</details>", marker_pos)
    assert end != -1
    return html[section_start : end + len("</details>")]


class TestGoldenReposConfigSectionExternallyManagedCheckbox:
    """The golden_repos edit form must expose a checkbox for the flag."""

    def test_section_contains_checkbox_input(self) -> None:
        section = _read_golden_repos_section()
        assert 'name="externally_managed"' in section, (
            "config_section.html golden_repos edit form must include an "
            "input named externally_managed"
        )
        assert 'type="checkbox"' in section

    def test_checkbox_has_preceding_hidden_fallback(self) -> None:
        """
        Unchecking a bare checkbox omits the key from form POST data entirely
        (browser behavior), which would make the flag impossible to turn OFF
        via the Web UI. A hidden fallback input with the same name, placed
        BEFORE the checkbox in DOM order, guarantees the key is always
        submitted (last value wins) -- same pattern as Story #1412's
        temporal_all_branches_enabled field.
        """
        section = _read_golden_repos_section()
        checkbox_marker = 'type="checkbox"'
        checkbox_pos = section.find(checkbox_marker)
        name_pos = section.find('name="externally_managed"', checkbox_pos)
        assert checkbox_pos != -1 and name_pos != -1, (
            "Expected a type=checkbox input named externally_managed"
        )
        hidden_marker = 'type="hidden" name="externally_managed" value="false"'
        hidden_pos = section.find(hidden_marker)
        assert hidden_pos != -1, (
            f"Expected a hidden fallback input ({hidden_marker!r}) before the checkbox"
        )
        assert hidden_pos < checkbox_pos, (
            "Hidden fallback input must appear BEFORE the checkbox in DOM order "
            "so the checked checkbox's 'true' value wins when checked."
        )

    def test_display_row_references_externally_managed_value(self) -> None:
        section = _read_golden_repos_section()
        assert "config.golden_repos.externally_managed" in section, (
            "Display mode must show the current externally_managed value"
        )


class TestValidateConfigSectionGoldenReposExternallyManaged:
    """_validate_config_section must accept the externally_managed field."""

    def test_accepts_true(self) -> None:
        error = _validate_config_section("golden_repos", {"externally_managed": "true"})
        assert error is None

    def test_accepts_false(self) -> None:
        error = _validate_config_section(
            "golden_repos", {"externally_managed": "false"}
        )
        assert error is None

    def test_accepts_alongside_other_golden_repos_fields(self) -> None:
        error = _validate_config_section(
            "golden_repos",
            {
                "refresh_interval_seconds": "120",
                "analysis_model": "sonnet",
                "externally_managed": "true",
            },
        )
        assert error is None
