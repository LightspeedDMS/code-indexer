"""Tests for Story #1397: HNSW orphan-repair sweep Web UI Config route layer.

Covers:
  - "hnsw_orphan_sweep" membership in _VALID_CONFIG_SECTIONS (otherwise every
    POST /admin/config/hnsw_orphan_sweep returns HTTP 400 "Invalid section").
  - _validate_config_section("hnsw_orphan_sweep", ...): rejects out-of-range
    hours (0-23), tick_interval_minutes < 1, batch_size < 1.
  - _get_current_config() surfaces the "hnsw_orphan_sweep" section (mirrors
    test_admin_config_render_1179.py's _call_get_current_config helper).
  - A REAL route-level POST/display round trip driving the actual
    update_config_section() handler end-to-end (route -> ConfigService),
    per the issue's explicit warning that a dataclass round-trip test alone
    would NOT catch a missing ConfigService category -- mirrors
    test_search_events_config_save.py::TestUpdateConfigSectionHandlerDirect.
"""

import asyncio
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Section membership
# ---------------------------------------------------------------------------


class TestHnswOrphanSweepSectionMembership:
    def test_hnsw_orphan_sweep_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "hnsw_orphan_sweep" in _VALID_CONFIG_SECTIONS, (
            "hnsw_orphan_sweep must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/hnsw_orphan_sweep would otherwise always "
            "return HTTP 400 'Invalid section: hnsw_orphan_sweep'."
        )


# ---------------------------------------------------------------------------
# _validate_config_section validation logic
# ---------------------------------------------------------------------------


def _validate(data: dict):
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("hnsw_orphan_sweep", data)


class TestValidateConfigSectionAcceptsValidInput:
    def test_valid_full_payload_returns_none(self) -> None:
        assert (
            _validate(
                {
                    "operating_hours_start_utc": 22,
                    "operating_hours_end_utc": 6,
                    "tick_interval_minutes": 10,
                    "batch_size": 10,
                }
            )
            is None
        )

    def test_missing_fields_returns_none(self) -> None:
        # Partial save must not error.
        assert _validate({}) is None

    def test_boundary_hour_values_accepted(self) -> None:
        assert (
            _validate({"operating_hours_start_utc": 0, "operating_hours_end_utc": 23})
            is None
        )


class TestValidateConfigSectionRejectsOutOfRangeHours:
    def test_start_hour_25_rejected(self) -> None:
        error = _validate({"operating_hours_start_utc": 25})
        assert error is not None

    def test_end_hour_negative_rejected(self) -> None:
        error = _validate({"operating_hours_end_utc": -1})
        assert error is not None

    def test_start_hour_24_rejected(self) -> None:
        error = _validate({"operating_hours_start_utc": 24})
        assert error is not None


class TestValidateConfigSectionRejectsInvalidCadenceAndBatch:
    def test_tick_interval_minutes_zero_rejected(self) -> None:
        error = _validate({"tick_interval_minutes": 0})
        assert error is not None

    def test_tick_interval_minutes_negative_rejected(self) -> None:
        error = _validate({"tick_interval_minutes": -5})
        assert error is not None

    def test_batch_size_zero_rejected(self) -> None:
        error = _validate({"batch_size": 0})
        assert error is not None


# ---------------------------------------------------------------------------
# _get_current_config() surfaces the section (mirrors Bug #1179's pattern)
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


class TestGetCurrentConfigSurfacesHnswOrphanSweep:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert "hnsw_orphan_sweep" in result

    def test_section_has_default_values(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result["hnsw_orphan_sweep"]
        assert section["enabled"] is True
        assert section["operating_hours_start_utc"] == 0
        assert section["operating_hours_end_utc"] == 0
        assert section["tick_interval_minutes"] == 7
        assert section["batch_size"] == 15


# ---------------------------------------------------------------------------
# Real route-level POST round trip -- drives the ACTUAL update_config_section
# coroutine so a missing ConfigService category is caught (issue's explicit
# warning: a dataclass round-trip test alone would NOT catch this gap).
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
    def test_post_all_five_fields_persists_via_real_config_service(
        self, tmp_path
    ) -> None:
        """Drives the real update_config_section() handler with all 5
        fields. Before the ConfigService plumbing existed, update_setting()
        would raise ValueError("Unknown category: hnsw_orphan_sweep") and
        the handler would return an error response -- this is exactly the
        #1395 gap this story closes."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "hnsw_orphan_sweep",
            {
                "enabled": "true",
                "operating_hours_start_utc": "22",
                "operating_hours_end_utc": "6",
                "tick_interval_minutes": "10",
                "batch_size": "10",
            },
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response -- ConfigService plumbing "
            f"may be missing: {body!r}"
        )

        cfg = svc.get_config().hnsw_orphan_repair_sweep_config
        assert cfg.enabled is True
        assert cfg.operating_hours_start_utc == 22
        assert cfg.operating_hours_end_utc == 6
        assert cfg.tick_interval_minutes == 10
        assert cfg.batch_size == 10

    def test_post_enabled_false_disables_sweep(self, tmp_path) -> None:
        """Gotcha #1: an explicit enabled=false POST must persist False --
        proving the boolean <select> (not an unchecked checkbox) round
        trips correctly through the real route + ConfigService."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "hnsw_orphan_sweep",
            {
                "enabled": "false",
                "operating_hours_start_utc": "0",
                "operating_hours_end_utc": "0",
                "tick_interval_minutes": "7",
                "batch_size": "15",
            },
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body

        cfg = svc.get_config().hnsw_orphan_repair_sweep_config
        assert cfg.enabled is False

    def test_post_invalid_hour_is_rejected_and_not_saved(self, tmp_path) -> None:
        """Validation runs before save; an out-of-range hour must be
        rejected and the original config left untouched."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "hnsw_orphan_sweep",
            {"operating_hours_start_utc": "25"},
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" in body or body != "ok"
        cfg = svc.get_config().hnsw_orphan_repair_sweep_config
        assert cfg.operating_hours_start_utc == 0  # untouched default


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
