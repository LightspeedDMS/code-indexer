"""Tests for Issue #1753: Content Limits admin config Save round trip.

Root cause: "content_limits" is present in `_VALID_CONFIG_SECTIONS`
(routes.py) and passes `_validate_config_section`, but
`ConfigService._apply_setting` has no "content_limits" branch, so
`update_settings_atomic` raises `ValueError: Unknown category:
content_limits`. The admin Config page's Content Limits section (fixed to
DISPLAY real persisted values by Issue #1750) renders a live Edit form that
POSTs to /admin/config/content_limits, but clicking Save always fails.

Mirrors test_search_timeouts_config_route_1398.py's exact structure -- a
REAL route-level POST/display round trip driving the actual
update_config_section() handler end-to-end (route -> ConfigService), per
that issue's own warning that a dataclass round-trip test alone would NOT
catch a missing ConfigService category.
"""

import asyncio
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Section membership (already true pre-fix -- content_limits was correctly
# added to _VALID_CONFIG_SECTIONS by Story #32; the bug is downstream in
# ConfigService._apply_setting).
# ---------------------------------------------------------------------------


class TestContentLimitsSectionMembership:
    def test_content_limits_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "content_limits" in _VALID_CONFIG_SECTIONS, (
            "content_limits must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/content_limits would otherwise always "
            "return HTTP 400 'Invalid section: content_limits'."
        )


# ---------------------------------------------------------------------------
# Real route-level POST round trip -- drives the ACTUAL update_config_section
# coroutine so a missing ConfigService category is caught (the exact gap
# this issue is about).
# ---------------------------------------------------------------------------


def _make_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    return ConfigService(config_manager=mgr)


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
        return HTMLResponse(content=body)

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


class TestRealRoutePostRoundTrip:
    def test_post_all_six_fields_persists_via_real_config_service(
        self, tmp_path
    ) -> None:
        """Drives the real update_config_section() handler with all 6
        content_limits fields. Before the ConfigService plumbing existed,
        update_settings_atomic() -> _apply_setting() would raise
        ValueError("Unknown category: content_limits") and the handler
        would return an error response instead of persisting anything.

        This is a FULL round trip: save via the route handler, then
        re-read via ConfigService.get_config() to prove the new values are
        genuinely persisted, not merely "no exception raised".
        """
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "content_limits",
            {
                "chars_per_token": "7",
                "file_content_max_tokens": "12345",
                "git_diff_max_tokens": "23456",
                "git_log_max_tokens": "34567",
                "search_result_max_tokens": "45678",
                "cache_ttl_seconds": "9999",
            },
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response -- ConfigService plumbing "
            f"may be missing: {body!r}"
        )

        # Round-trip proof: re-read through a FRESH ConfigService instance
        # backed by the same on-disk store, so this cannot pass merely
        # because the in-memory object was mutated without a real save.
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        fresh_mgr = ServerConfigManager(server_dir_path=str(tmp_path))
        fresh_svc = ConfigService(config_manager=fresh_mgr)
        cfg = fresh_svc.get_config().content_limits_config
        assert cfg is not None
        assert cfg.chars_per_token == 7
        assert cfg.file_content_max_tokens == 12345
        assert cfg.git_diff_max_tokens == 23456
        assert cfg.git_log_max_tokens == 34567
        assert cfg.search_result_max_tokens == 45678
        assert cfg.cache_ttl_seconds == 9999

    def test_post_partial_fields_persists_only_submitted_values(self, tmp_path) -> None:
        """A partial save (2-3 of the 6 fields, as the issue instructs)
        must persist exactly those fields and leave the rest untouched."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "content_limits",
            {
                "chars_per_token": "5",
                "cache_ttl_seconds": "7200",
            },
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response on a partial save: {body!r}"
        )

        cfg = svc.get_config().content_limits_config
        assert cfg is not None
        assert cfg.chars_per_token == 5
        assert cfg.cache_ttl_seconds == 7200
        # Untouched fields keep their compiled-in defaults.
        assert cfg.file_content_max_tokens == 50000
        assert cfg.git_diff_max_tokens == 50000
        assert cfg.git_log_max_tokens == 50000
        assert cfg.search_result_max_tokens == 50000

    def test_post_invalid_value_is_rejected_and_not_saved(self, tmp_path) -> None:
        """Validation runs before save; an out-of-range value must be
        rejected and the original config left untouched."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "content_limits",
            {"chars_per_token": "999"},
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" in body or body != "ok"
        cfg = svc.get_config().content_limits_config
        assert cfg is not None
        assert cfg.chars_per_token == 4  # untouched default


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
