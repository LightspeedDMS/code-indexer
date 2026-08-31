"""Structural tests for Story #1458 (Epic #1454) round-6 item #10: Fleet
Migration Web UI Config section.

Mirrors tests/unit/server/web/test_config_section_search_timeouts_1398.py's
established structural-test pattern exactly (source-text extraction, no
template rendering engine needed).
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
    start = html.find('id="section-fleet-migration"')
    assert start != -1, "Missing Fleet Migration <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def test_template_contains_fleet_migration_section():
    section = _extract_section(_read_template())
    assert "fleet migration" in section.lower()


def test_template_contains_all_field_inputs():
    section = _extract_section(_read_template())
    for field_name in ("enabled", "tick_interval_minutes", "canary_gate_enabled"):
        assert f'name="{field_name}"' in section, f"Missing input for {field_name}"


def test_template_posts_to_admin_config_fleet_migration():
    section = _extract_section(_read_template())
    assert 'action="/admin/config/fleet_migration"' in section
