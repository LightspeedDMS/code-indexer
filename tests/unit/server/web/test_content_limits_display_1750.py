"""Route-level display tests for Issue #1750: the admin Config page's
Content Limits section must reflect the REAL persisted state, not the
compiled-in class default.

Root cause (see test_config_service_content_limits_1750.py for the
service-layer fix): a `_content_limits_settings(config)` helper did not
exist and was never invoked from `get_all_settings()`. `routes.py`'s
`settings.get("content_limits", asdict(ContentLimitsConfig()))` therefore
always fell back to the class default, regardless of the true persisted
`ServerConfig.content_limits_config` state.

Unlike Issue #1749 (temporal_legacy_migration), this bug is display-only --
`ConfigService._apply_setting` has no "content_limits" branch, so submitting
the edit form fails loud with `ValueError: Unknown category: content_limits`
rather than silently persisting the wrong value. These tests therefore only
prove the DISPLAY reaches the real persisted state, not a save round-trip.

Mirrors the established patterns in test_admin_config_render_1179.py
(_get_current_config() + real Jinja template render) and
test_temporal_legacy_migration_display_1749.py (Issue #1749's sibling fix).
"""

import re
import unittest.mock as mock

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.web import routes


def _get_current_config_with(svc: ConfigService) -> dict:
    """Call routes._get_current_config() against a real ConfigService backed
    by a temp SQLite DB, mirroring test_admin_config_render_1179.py's
    established patching pattern.
    """
    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        return routes._get_current_config()


def _build_render_context(config: dict) -> dict:
    """Minimal template context matching config_section_partial() (verified
    against routes.py:8938-8948 by test_admin_config_render_1179.py)."""
    return {
        "request": None,
        "csrf_token": "test_csrf_token",
        "config": config,
        "validation_errors": {},
        "restart_required_fields": [],
        "api_keys_status": {},
        "github_token_data": None,
        "gitlab_token_data": None,
    }


def _extract_section(html: str) -> str:
    start = html.find('id="section-content_limits"')
    assert start != -1, "Missing Content Limits <details> section"
    section_start = html.rfind("<details", 0, start)
    assert section_start != -1
    end = html.find("</details>", start)
    assert end != -1
    return html[section_start : end + len("</details>")]


def _render_content_limits_section(config: dict) -> str:
    template = routes.templates.env.get_template("partials/config_section.html")
    html = template.render(**_build_render_context(config))
    return _extract_section(html)


class TestGetCurrentConfigReflectsPersistedContentLimits:
    """_get_current_config() -- the dict fed to the Jinja template -- must
    reflect the true persisted state, not the class default."""

    def test_reflects_persisted_chars_per_token(self, tmp_path) -> None:
        svc = ConfigService(server_dir_path=str(tmp_path))
        server_config = svc.load_config()
        assert server_config.content_limits_config is not None
        server_config.content_limits_config.chars_per_token = 7
        svc.save_config(server_config)

        # Fresh instance forces a real reload from the persisted store.
        fresh_svc = ConfigService(server_dir_path=str(tmp_path))
        config = _get_current_config_with(fresh_svc)

        assert config["content_limits"]["chars_per_token"] == 7, (
            "_get_current_config()['content_limits']['chars_per_token'] "
            "does not reflect the persisted value 7 -- admin Config page "
            "would silently display the compiled-in default (4)."
        )


class TestConfigPageRenderReflectsPersistedContentLimits:
    """Render-level proof: the REAL Jinja template must display the real
    persisted value, not the hardcoded default, for every content_limits
    field."""

    def test_display_cells_show_persisted_values(self, tmp_path) -> None:
        svc = ConfigService(server_dir_path=str(tmp_path))
        server_config = svc.load_config()
        cl = server_config.content_limits_config
        assert cl is not None
        cl.chars_per_token = 7
        cl.file_content_max_tokens = 12345
        cl.git_diff_max_tokens = 23456
        cl.git_log_max_tokens = 34567
        cl.search_result_max_tokens = 45678
        cl.cache_ttl_seconds = 9999
        svc.save_config(server_config)

        fresh_svc = ConfigService(server_dir_path=str(tmp_path))
        config = _get_current_config_with(fresh_svc)
        section_html = _render_content_limits_section(config)

        for value in (7, 12345, 23456, 34567, 45678, 9999):
            assert re.search(rf'<td class="config-value">{value}</td>', section_html), (
                f"Config page display does not show persisted value {value} "
                "in the Content Limits section -- still showing compiled-in "
                "defaults."
            )

    def test_edit_form_inputs_prefill_persisted_values(self, tmp_path) -> None:
        """The exact harm the issue calls out: the six <input> fields
        (config_section.html:1690-1758) must pre-fill from the real
        persisted state, not from asdict(ContentLimitsConfig())'s defaults.
        """
        svc = ConfigService(server_dir_path=str(tmp_path))
        server_config = svc.load_config()
        assert server_config.content_limits_config is not None
        server_config.content_limits_config.file_content_max_tokens = 98765
        svc.save_config(server_config)

        fresh_svc = ConfigService(server_dir_path=str(tmp_path))
        config = _get_current_config_with(fresh_svc)
        section_html = _render_content_limits_section(config)

        assert re.search(
            r'name="file_content_max_tokens" value="98765"', section_html
        ), (
            'Edit-mode <input name="file_content_max_tokens"> does not '
            "pre-fill from the persisted value 98765 -- still hardcoded to "
            "the compiled-in default (50000)."
        )
