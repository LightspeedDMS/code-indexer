"""Structural tests for Story #926 cidx-meta backup config section."""

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
    start = html.find('id="section-cidx-meta-backup"')
    assert start != -1, "Missing cidx-meta backup <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def test_template_contains_cidx_meta_backup_section():
    """# Story #926 AC1: config template includes a dedicated cidx-meta backup section."""
    section = _extract_section(_read_template())
    assert "cidx-meta backup" in section.lower()


def test_template_contains_enabled_select_and_remote_url_input():
    """# Story #926 AC1: section exposes enabled toggle and remote_url field."""
    section = _extract_section(_read_template())
    assert 'name="enabled"' in section
    assert 'name="remote_url"' in section


def test_template_posts_to_admin_config_cidx_meta_backup():
    """# Story #926 AC1: section posts to the dedicated /admin/config/cidx_meta_backup route."""
    section = _extract_section(_read_template())
    assert 'action="/admin/config/cidx_meta_backup"' in section
