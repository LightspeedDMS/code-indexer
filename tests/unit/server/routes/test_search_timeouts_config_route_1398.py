"""Tests for Issue #1398: Query & Search Timeouts Web UI Config route layer.

Mirrors test_hnsw_orphan_sweep_config_route_1397.py's exact structure:

  - "search_timeouts" membership in _VALID_CONFIG_SECTIONS (otherwise every
    POST /admin/config/search_timeouts returns HTTP 400 "Invalid section").
  - _validate_config_section("search_timeouts", ...): rejects out-of-range
    values for all 5 fields.
  - _get_current_config() surfaces the "search_timeouts" section.
  - A REAL route-level POST/display round trip driving the actual
    update_config_section() handler end-to-end (route -> ConfigService),
    per the issue's explicit warning that a dataclass round-trip test alone
    would NOT catch a missing ConfigService category.
"""

import asyncio
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Section membership
# ---------------------------------------------------------------------------


class TestSearchTimeoutsSectionMembership:
    def test_search_timeouts_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "search_timeouts" in _VALID_CONFIG_SECTIONS, (
            "search_timeouts must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/search_timeouts would otherwise always "
            "return HTTP 400 'Invalid section: search_timeouts'."
        )


# ---------------------------------------------------------------------------
# _validate_config_section validation logic
# ---------------------------------------------------------------------------


def _validate(data: dict):
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("search_timeouts", data)


class TestValidateConfigSectionAcceptsValidInput:
    def test_valid_full_payload_returns_none(self) -> None:
        assert (
            _validate(
                {
                    "search_code_handler_timeout_seconds": 200,
                    "default_handler_timeout_seconds": 70,
                    "write_mode_handler_timeout_seconds": 800,
                    "embedding_provider_timeout_seconds": 40,
                    "reranker_timeout_seconds": 20,
                }
            )
            is None
        )

    def test_missing_fields_returns_none(self) -> None:
        # Partial save must not error.
        assert _validate({}) is None

    def test_boundary_values_accepted(self) -> None:
        assert (
            _validate(
                {
                    "search_code_handler_timeout_seconds": 30,
                    "default_handler_timeout_seconds": 300,
                }
            )
            is None
        )


class TestValidateConfigSectionRejectsOutOfRange:
    def test_search_code_handler_timeout_too_low_rejected(self) -> None:
        error = _validate({"search_code_handler_timeout_seconds": 10})
        assert error is not None

    def test_search_code_handler_timeout_too_high_rejected(self) -> None:
        error = _validate({"search_code_handler_timeout_seconds": 9999})
        assert error is not None

    def test_default_handler_timeout_too_low_rejected(self) -> None:
        error = _validate({"default_handler_timeout_seconds": 1})
        assert error is not None

    def test_write_mode_handler_timeout_too_low_rejected(self) -> None:
        error = _validate({"write_mode_handler_timeout_seconds": 5})
        assert error is not None

    def test_embedding_provider_timeout_too_low_rejected(self) -> None:
        error = _validate({"embedding_provider_timeout_seconds": 0})
        assert error is not None

    def test_reranker_timeout_too_low_rejected(self) -> None:
        error = _validate({"reranker_timeout_seconds": 0})
        assert error is not None

    def test_non_numeric_value_rejected(self) -> None:
        error = _validate({"search_code_handler_timeout_seconds": "not-a-number"})
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


class TestGetCurrentConfigSurfacesSearchTimeouts:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert "search_timeouts" in result

    def test_section_has_default_values(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result["search_timeouts"]
        assert section["search_code_handler_timeout_seconds"] == 180
        assert section["default_handler_timeout_seconds"] == 60
        assert section["write_mode_handler_timeout_seconds"] == 720
        assert section["embedding_provider_timeout_seconds"] == 30
        assert section["reranker_timeout_seconds"] == 15


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
    def test_post_all_five_fields_persists_via_real_config_service(
        self, tmp_path
    ) -> None:
        """Drives the real update_config_section() handler with all 5
        fields. Before the ConfigService plumbing existed, update_setting()
        would raise ValueError("Unknown category: search_timeouts") and the
        handler would return an error response."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "search_timeouts",
            {
                "search_code_handler_timeout_seconds": "200",
                "default_handler_timeout_seconds": "70",
                "write_mode_handler_timeout_seconds": "800",
                "embedding_provider_timeout_seconds": "40",
                "reranker_timeout_seconds": "20",
            },
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response -- ConfigService plumbing "
            f"may be missing: {body!r}"
        )

        cfg = svc.get_config().search_timeouts_config
        assert cfg.search_code_handler_timeout_seconds == 200
        assert cfg.default_handler_timeout_seconds == 70
        assert cfg.write_mode_handler_timeout_seconds == 800
        assert cfg.embedding_provider_timeout_seconds == 40
        assert cfg.reranker_timeout_seconds == 20

    def test_post_invalid_value_is_rejected_and_not_saved(self, tmp_path) -> None:
        """Validation runs before save; an out-of-range value must be
        rejected and the original config left untouched."""
        svc = _make_service(str(tmp_path))

        response = _run_update_config_section(
            "search_timeouts",
            {"search_code_handler_timeout_seconds": "1"},
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" in body or body != "ok"
        cfg = svc.get_config().search_timeouts_config
        assert cfg.search_code_handler_timeout_seconds == 180  # untouched default


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
