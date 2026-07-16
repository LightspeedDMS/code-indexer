"""Structural tests for Issue #1398 Query & Search Timeouts config section."""

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
    start = html.find('id="section-search-timeouts"')
    assert start != -1, "Missing Query & Search Timeouts <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def _extract_field_label_block(section: str, field_name: str) -> str:
    """Return the <label>...</label> block whose input has name=field_name."""
    marker = f'name="{field_name}"'
    input_pos = section.find(marker)
    assert input_pos != -1, f"Missing input for {field_name}"
    label_start = section.rfind("<label", 0, input_pos)
    assert label_start != -1, f"No enclosing <label> found for {field_name}"
    label_end = section.find("</label>", input_pos)
    assert label_end != -1, f"No closing </label> found for {field_name}"
    return section[label_start : label_end + len("</label>")]


def test_template_contains_search_timeouts_section():
    section = _extract_section(_read_template())
    assert "query & search timeouts" in section.lower()


def test_template_contains_all_five_field_inputs():
    section = _extract_section(_read_template())
    for field_name in (
        "search_code_handler_timeout_seconds",
        "default_handler_timeout_seconds",
        "write_mode_handler_timeout_seconds",
        "embedding_provider_timeout_seconds",
        "reranker_timeout_seconds",
    ):
        assert f'name="{field_name}"' in section, f"Missing input for {field_name}"


def test_template_posts_to_admin_config_search_timeouts():
    section = _extract_section(_read_template())
    assert 'action="/admin/config/search_timeouts"' in section


def test_default_handler_timeout_label_notes_sync_dispatch_only():
    """The Web UI label for default_handler_timeout_seconds specifically
    (not just the section generally) must explicitly note the sync/async
    dispatch distinction (does NOT apply to regex_search or other
    async-handler tools) -- required by the issue's Fix Implementation
    checklist."""
    section = _extract_section(_read_template())
    label_block = _extract_field_label_block(section, "default_handler_timeout_seconds")
    lowered = label_block.lower()
    assert "regex_search" in lowered, (
        f"default_handler_timeout_seconds label must mention regex_search "
        f"as the async-dispatch exception: {label_block!r}"
    )
    assert "async" in lowered, (
        f"default_handler_timeout_seconds label must mention the async "
        f"dispatch distinction: {label_block!r}"
    )
