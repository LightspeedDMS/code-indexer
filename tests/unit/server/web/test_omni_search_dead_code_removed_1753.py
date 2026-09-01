"""Tests for Issue #1753: omni_search is genuinely dead config -- remove it.

Per Issue #1750's own investigation, "omni_search" has zero template
consumers (confirmed independently while fixing #1753 -- grep of
src/code_indexer/server/web/templates/ finds no reference to a
`config.omni_search` key or a `section-omni_search` id; the "MCP
Omni-Search Settings" heading in config_section.html belongs to the
UNRELATED "multi_search" section, per Story #29's OmniSearchConfig ->
multi_search_limits_config merge). Unlike content_limits (which IS
live-consumed by file_service.py / mcp/handlers/files.py and just needed
its save path wired), omni_search's correct resolution is REMOVAL of its
3 routes.py references:
  1. The _VALID_CONFIG_SECTIONS allowlist entry
  2. The _get_current_config() settings.get("omni_search", {}) dict entry
  3. The _validate_config_section elif "omni_search" branch

This is a SEPARATE fix for a SEPARATE root cause from content_limits (dead
code vs. genuinely unwired-but-live code) that happened to surface the
same symptom (ValueError: Unknown category: omni_search on save).
"""

import asyncio
import unittest.mock as mock

import pytest


class TestOmniSearchRemovedFromValidSections:
    def test_omni_search_not_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "omni_search" not in _VALID_CONFIG_SECTIONS, (
            "omni_search is dead config (zero template consumers, per "
            "Issue #1750's investigation) and must be removed from "
            "_VALID_CONFIG_SECTIONS, not left as an unreachable allowlist "
            "entry that _apply_setting can never actually service."
        )


class TestGetCurrentConfigNoLongerSurfacesOmniSearch:
    def test_omni_search_key_absent(self, tmp_path) -> None:
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager
        from code_indexer.server.web import routes

        mgr = ServerConfigManager(server_dir_path=str(tmp_path))
        svc = ConfigService(config_manager=mgr)

        with mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ):
            config = routes._get_current_config()

        assert "omni_search" not in config, (
            "_get_current_config() still surfaces a dead 'omni_search' "
            "key -- the admin Config page has no template consumer for "
            "it, so this dict entry is pure dead weight."
        )


# ---------------------------------------------------------------------------
# Real route-level POST -- omni_search must now fail loud with a clean
# "Invalid section" 400 (gated by _VALID_CONFIG_SECTIONS), never reach
# ConfigService and raise the confusing "Unknown category" ValueError.
# ---------------------------------------------------------------------------


def _build_fake_request(form_data: dict):
    from unittest.mock import AsyncMock, MagicMock
    from starlette.datastructures import ImmutableMultiDict

    req = MagicMock()
    req.session = {}
    items = list(form_data.items())
    multi = ImmutableMultiDict(items)
    req.form = AsyncMock(return_value=multi)
    return req


def _run_update_config_section(section: str, form_data: dict, svc):
    from fastapi.responses import HTMLResponse
    from code_indexer.server.web.routes import update_config_section

    fake_session = mock.MagicMock()
    fake_session.username = "admin"
    fake_session.role = "admin"

    req = _build_fake_request(form_data)

    def _fake_page_response(request, session, **kwargs):
        body = kwargs.get("error_message") or kwargs.get("success_message") or "ok"
        status_code = kwargs.get("status_code", 200)
        return HTMLResponse(content=body, status_code=status_code)

    with (
        mock.patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=fake_session,
        ),
        mock.patch(
            "code_indexer.server.web.routes.validate_login_csrf_token",
            return_value=True,
        ),
        mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ),
        mock.patch(
            "code_indexer.server.web.routes._create_config_page_response",
            side_effect=_fake_page_response,
        ),
    ):
        response = asyncio.run(
            update_config_section(
                request=req, section=section, csrf_token="dummy-token"
            )
        )
    return response


class TestPostToOmniSearchRejectedAsInvalidSection:
    def test_post_returns_invalid_section_not_unknown_category(self, tmp_path) -> None:
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(server_dir_path=str(tmp_path))
        svc = ConfigService(config_manager=mgr)

        with mock.patch.object(
            svc, "update_settings_atomic", wraps=svc.update_settings_atomic
        ) as spy_update:
            response = _run_update_config_section(
                "omni_search",
                {"max_workers": "10"},
                svc,
            )

        assert response.status_code == 400, (
            f"POST /admin/config/omni_search must be rejected with HTTP "
            f"400 at the _VALID_CONFIG_SECTIONS gate, got "
            f"{response.status_code}"
        )

        body = response.body.decode()
        assert "Invalid section" in body, (
            f"POST /admin/config/omni_search must be rejected at the "
            f"_VALID_CONFIG_SECTIONS gate with a clean 'Invalid section' "
            f"400, not reach ConfigService: {body!r}"
        )
        assert "Unknown category" not in body, (
            "omni_search must never reach ConfigService._apply_setting "
            f"after removal from _VALID_CONFIG_SECTIONS: {body!r}"
        )
        spy_update.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
