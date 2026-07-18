"""Tests for the Web UI 'embedding_stats' config section (Story #1418 Phase 3).

Mirrors the exact structure established for Issue #1398's search_timeouts
section: _VALID_CONFIG_SECTIONS registration, _get_current_config() default
fallback, _validate_config_section() range checks, and the Jinja template
(config_section.html) display + edit-mode form.
"""

from pathlib import Path


# ---------------------------------------------------------------------------
# _VALID_CONFIG_SECTIONS registration
# ---------------------------------------------------------------------------


class TestValidConfigSectionsIncludesEmbeddingStats:
    def test_embedding_stats_is_a_valid_section(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "embedding_stats" in _VALID_CONFIG_SECTIONS


# ---------------------------------------------------------------------------
# _get_current_config() default fallback
# ---------------------------------------------------------------------------


def _make_service(tmp_path):
    from code_indexer.server.services.config_service import ConfigService

    return ConfigService(server_dir_path=str(tmp_path))


class TestGetCurrentConfigIncludesEmbeddingStats:
    def test_section_present_with_defaults(self, tmp_path) -> None:
        import unittest.mock as mock
        from code_indexer.server.web import routes

        svc = _make_service(tmp_path)
        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ):
            config = routes._get_current_config()

        assert "embedding_stats" in config
        assert config["embedding_stats"]["enabled"] is True
        assert config["embedding_stats"]["flush_interval_seconds"] == 30.0
        assert config["embedding_stats"]["retention_days"] == 90


# ---------------------------------------------------------------------------
# _validate_config_section() range checks
# ---------------------------------------------------------------------------


class TestValidateConfigSectionEmbeddingStats:
    def test_valid_values_pass(self) -> None:
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section(
            "embedding_stats",
            {"enabled": "true", "flush_interval_seconds": "30", "retention_days": "90"},
        )
        assert error is None

    def test_flush_interval_seconds_zero_rejected(self) -> None:
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section(
            "embedding_stats", {"flush_interval_seconds": "0"}
        )
        assert error is not None

    def test_flush_interval_seconds_non_numeric_rejected(self) -> None:
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section(
            "embedding_stats", {"flush_interval_seconds": "not-a-number"}
        )
        assert error is not None

    def test_retention_days_zero_rejected(self) -> None:
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section("embedding_stats", {"retention_days": "0"})
        assert error is not None

    def test_retention_days_negative_rejected(self) -> None:
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section("embedding_stats", {"retention_days": "-1"})
        assert error is not None

    def test_retention_days_non_numeric_rejected(self) -> None:
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section(
            "embedding_stats", {"retention_days": "not-a-number"}
        )
        assert error is not None


# ---------------------------------------------------------------------------
# Jinja template structural coverage
# ---------------------------------------------------------------------------


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
    start = html.find('id="section-embedding-stats"')
    assert start != -1, "Missing Embedding Stats <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def test_template_contains_embedding_stats_section():
    section = _extract_section(_read_template())
    assert "embedding" in section.lower()


def test_template_contains_all_three_field_inputs():
    section = _extract_section(_read_template())
    for field_name in ("enabled", "flush_interval_seconds", "retention_days"):
        assert f'name="{field_name}"' in section, f"Missing input for {field_name}"


def test_template_posts_to_admin_config_embedding_stats():
    section = _extract_section(_read_template())
    assert 'action="/admin/config/embedding_stats"' in section


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
