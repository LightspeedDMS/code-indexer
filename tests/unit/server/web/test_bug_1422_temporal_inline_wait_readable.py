"""Bug #1422 -- temporal_inline_wait_seconds missing from Web UI Config
screen (write-only, no read surface).

`temporal_inline_wait_seconds` is the 6th field of SearchTimeoutsConfig
(added by Story #1400, `src/code_indexer/server/utils/config_manager.py`).
The POST/write path (`_validate_config_section`, `ConfigService.
update_setting`) already accepts and persists it. This bug is about the
READ side:

  (a) the dict-level read surfaces (`ConfigService.get_all_settings()
      ["search_timeouts"]` and `routes._get_current_config()
      ["search_timeouts"]`) -- confirmed here to ALREADY include the field
      (Story #1400 wired `_search_timeouts_settings()` correctly). These
      assertions lock in that already-correct behavior as a regression
      guard.

  (b) the Jinja template (`config_section.html`) -- confirmed to be
      MISSING the field entirely from both the read-only display table and
      the edit-mode form, unlike its 5 siblings (search_code_handler_
      timeout_seconds, default_handler_timeout_seconds, write_mode_
      handler_timeout_seconds, embedding_provider_timeout_seconds,
      reranker_timeout_seconds). This is the actual bug -- these are the
      RED tests before the fix.
"""

from pathlib import Path


# ---------------------------------------------------------------------------
# (a) Dict-level read-surface regression guards
# ---------------------------------------------------------------------------


def _make_service(tmp_path):
    from code_indexer.server.services.config_service import ConfigService

    return ConfigService(server_dir_path=str(tmp_path))


class TestGetAllSettingsIncludesTemporalInlineWait:
    def test_field_present(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        section = svc.get_all_settings()["search_timeouts"]
        assert "temporal_inline_wait_seconds" in section

    def test_default_value(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        section = svc.get_all_settings()["search_timeouts"]
        assert section["temporal_inline_wait_seconds"] == 60.0


class TestGetCurrentConfigIncludesTemporalInlineWait:
    def test_field_present_in_routes_dict(self, tmp_path) -> None:
        import unittest.mock as mock
        from code_indexer.server.web import routes

        svc = _make_service(tmp_path)
        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ):
            config = routes._get_current_config()
        assert "temporal_inline_wait_seconds" in config["search_timeouts"]


# ---------------------------------------------------------------------------
# (b) Template render coverage -- the actual bug
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
    start = html.find('id="section-search-timeouts"')
    assert start != -1, "Missing Query & Search Timeouts <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


class TestTemplateRendersTemporalInlineWaitField:
    def test_display_mode_shows_value(self) -> None:
        section = _extract_section(_read_template())
        assert "config.search_timeouts.temporal_inline_wait_seconds" in section, (
            "Display table is missing the temporal_inline_wait_seconds value "
            "cell -- the 6th SearchTimeoutsConfig field must render alongside "
            "its 5 siblings"
        )

    def test_edit_form_has_input(self) -> None:
        section = _extract_section(_read_template())
        assert 'name="temporal_inline_wait_seconds"' in section, (
            "Edit form is missing an <input> for temporal_inline_wait_seconds "
            "-- the 6th SearchTimeoutsConfig field must be editable alongside "
            "its 5 siblings"
        )
