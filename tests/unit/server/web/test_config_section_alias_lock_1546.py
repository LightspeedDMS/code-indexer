"""Tests for the Web UI 'alias_lock' config section (Issue #1546 Phase 2).

Mirrors the structure established for Story #1458's fleet_migration
section (a single-purpose operator rollout gate): _VALID_CONFIG_SECTIONS
registration, _get_current_config() default fallback, and the Jinja
template (config_section.html) display + edit-mode form.
"""

from pathlib import Path


class TestValidConfigSectionsIncludesAliasLock:
    def test_alias_lock_is_a_valid_section(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "alias_lock" in _VALID_CONFIG_SECTIONS


def _make_service(tmp_path):
    from code_indexer.server.services.config_service import ConfigService

    return ConfigService(server_dir_path=str(tmp_path))


class TestGetCurrentConfigIncludesAliasLock:
    def test_section_present_with_defaults(self, tmp_path) -> None:
        import unittest.mock as mock
        from code_indexer.server.web import routes

        svc = _make_service(tmp_path)
        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ):
            config = routes._get_current_config()

        assert "alias_lock" in config
        assert config["alias_lock"]["db_backed_enabled"] is True


class TestValidateConfigSectionAliasLock:
    def test_boolean_only_section_never_rejects(self) -> None:
        """Mirrors temporal_legacy_migration's own validation: a
        boolean-only (Yes/No <select>) section needs no numeric range
        checks."""
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section("alias_lock", {"db_backed_enabled": "true"})
        assert error is None


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
    start = html.find('id="section-alias-lock"')
    assert start != -1, "Missing Alias Lock <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def test_template_contains_alias_lock_section():
    section = _extract_section(_read_template())
    assert "alias" in section.lower()


def test_template_contains_db_backed_enabled_input():
    section = _extract_section(_read_template())
    assert 'name="db_backed_enabled"' in section


def test_template_posts_to_admin_config_alias_lock():
    section = _extract_section(_read_template())
    assert 'action="/admin/config/alias_lock"' in section


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
