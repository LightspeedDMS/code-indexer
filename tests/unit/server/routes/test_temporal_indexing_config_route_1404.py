"""Tests for Story #1404: Global Temporal Indexing Floor Date Web UI Config
route layer.

Mirrors test_search_timeouts_config_route_1398.py's exact structure:

  - "temporal_indexing" membership in _VALID_CONFIG_SECTIONS (otherwise every
    POST /admin/config/temporal_indexing returns HTTP 400 "Invalid section").
  - _validate_config_section("temporal_indexing", ...): accepts None/empty/
    valid dates, rejects malformed/non-real/non-zero-padded dates.
  - _get_current_config() surfaces the "temporal_indexing" section.
  - A REAL route-level POST/display round trip driving the actual
    update_config_section() handler end-to-end (route -> ConfigService).
"""

import asyncio
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Section membership
# ---------------------------------------------------------------------------


class TestTemporalIndexingSectionMembership:
    def test_temporal_indexing_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "temporal_indexing" in _VALID_CONFIG_SECTIONS, (
            "temporal_indexing must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/temporal_indexing would otherwise always "
            "return HTTP 400 'Invalid section: temporal_indexing'."
        )


# ---------------------------------------------------------------------------
# _validate_config_section validation logic
# ---------------------------------------------------------------------------


def _validate(data: dict):
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("temporal_indexing", data)


class TestValidateConfigSectionAcceptsValidInput:
    def test_valid_date_returns_none(self) -> None:
        assert _validate({"index_floor_date": "2025-01-01"}) is None

    def test_empty_string_returns_none(self) -> None:
        assert _validate({"index_floor_date": ""}) is None

    def test_missing_field_returns_none(self) -> None:
        assert _validate({}) is None

    def test_none_value_returns_none(self) -> None:
        assert _validate({"index_floor_date": None}) is None


class TestValidateConfigSectionRejectsMalformed:
    def test_non_real_calendar_date_rejected(self) -> None:
        error = _validate({"index_floor_date": "2026-02-30"})
        assert error is not None

    def test_non_zero_padded_date_rejected(self) -> None:
        error = _validate({"index_floor_date": "2026-1-1"})
        assert error is not None

    def test_garbage_string_rejected(self) -> None:
        error = _validate({"index_floor_date": "not-a-date"})
        assert error is not None


# ---------------------------------------------------------------------------
# _get_current_config() surfaces the section
# ---------------------------------------------------------------------------


def _make_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    return ConfigService(config_manager=mgr)


def _call_get_current_config(svc) -> dict:
    from code_indexer.server.web import routes

    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        result: dict = routes._get_current_config()
        return result


class TestGetCurrentConfigSurfacesTemporalIndexing:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert "temporal_indexing" in result

    def test_section_has_default_value(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert result["temporal_indexing"]["index_floor_date"] is None


# ---------------------------------------------------------------------------
# Real route-level POST round trip -- drives the ACTUAL update_config_section
# coroutine so a missing ConfigService category is caught.
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
    def test_post_valid_date_persists_via_real_config_service(self, tmp_path) -> None:
        """Drives the real update_config_section() handler. Before the
        ConfigService plumbing existed, update_setting() would raise
        ValueError("Unknown category: temporal_indexing") and the handler
        would return an error response."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "temporal_indexing",
            {"index_floor_date": "2025-01-01"},
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response -- ConfigService plumbing "
            f"may be missing: {body!r}"
        )

        cfg = svc.get_config().temporal_indexing_config
        assert cfg.index_floor_date == "2025-01-01"

    def test_post_malformed_date_is_rejected_and_not_saved(self, tmp_path) -> None:
        """Validation runs before save; a malformed date must be rejected
        and the original config left untouched (Scenario 2)."""
        svc = _make_service(str(tmp_path))
        svc.update_setting("temporal_indexing", "index_floor_date", "2025-01-01")

        response = _run_update_config_section(
            "temporal_indexing",
            {"index_floor_date": "2026-02-30"},
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" in body or body != "ok"
        cfg = svc.get_config().temporal_indexing_config
        assert cfg.index_floor_date == "2025-01-01"  # untouched previous value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
