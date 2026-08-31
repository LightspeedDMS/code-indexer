"""Structural tests for Issue #1548: Temporal Legacy Migration Web UI
Config section.

Mirrors test_config_section_fleet_migration_1458.py's established
structural-test pattern exactly (source-text extraction, no template
rendering engine needed).
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
    start = html.find('id="section-temporal-legacy-migration"')
    assert start != -1, "Missing Temporal Legacy Migration <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def test_template_contains_temporal_legacy_migration_section():
    section = _extract_section(_read_template())
    assert "temporal legacy migration" in section.lower()


def test_template_contains_all_field_inputs():
    section = _extract_section(_read_template())
    for field_name in ("relocation_enabled", "cleanup_authorized"):
        assert f'name="{field_name}"' in section, f"Missing input for {field_name}"


def test_template_posts_to_admin_config_temporal_legacy_migration():
    section = _extract_section(_read_template())
    assert 'action="/admin/config/temporal_legacy_migration"' in section
