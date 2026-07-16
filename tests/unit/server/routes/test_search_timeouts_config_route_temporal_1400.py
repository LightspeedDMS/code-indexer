"""Tests for Story #1400 route-layer changes to the search_timeouts /
background_jobs Web UI config sections:

- _validate_config_section("search_timeouts", ...) accepts a float for
  temporal_inline_wait_seconds (extends the previously integer-only path).
- A real update_config_section() POST batch is atomic end-to-end: a field
  that individually PASSES the route's per-field range check but makes the
  overall candidate fail config_manager.validate_config's CROSS-FIELD check
  (CRITICAL 5 grace budget: temporal_inline_wait_seconds vs
  search_code_handler_timeout_seconds -- not checked by the route's
  per-field loop) must reject the WHOLE batch, leaving an accompanying
  individually-valid, unrelated field in the SAME submission unpersisted
  (Story #1400 CRITICAL 6, at the route layer -- mirrors
  test_search_timeouts_config_route_1398.py's real route-POST pattern).
"""

import asyncio
import unittest.mock as mock

import pytest


def _validate(data: dict):
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("search_timeouts", data)


class TestValidateConfigSectionAcceptsTemporalInlineWaitFloat:
    def test_float_value_accepted(self) -> None:
        assert _validate({"temporal_inline_wait_seconds": "30.5"}) is None

    def test_sub_second_value_accepted(self) -> None:
        assert _validate({"temporal_inline_wait_seconds": "0.001"}) is None

    def test_negative_value_rejected(self) -> None:
        error = _validate({"temporal_inline_wait_seconds": "-1.0"})
        assert error is not None

    def test_non_numeric_value_rejected(self) -> None:
        error = _validate({"temporal_inline_wait_seconds": "not-a-number"})
        assert error is not None


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


class TestRealRoutePostBatchAtomicity:
    def test_cross_field_rejection_leaves_accompanying_field_unpersisted(
        self, tmp_path
    ) -> None:
        """CRITICAL 6 at the route layer. Both submitted values INDIVIDUALLY
        pass the route's per-field range checks (default_handler_timeout_
        seconds=45 is in 10-300; temporal_inline_wait_seconds=179.999 is a
        valid non-negative float) -- the route-level validator never checks
        the CROSS-field grace-budget relationship. Only
        config_manager.validate_config's whole-candidate check catches that
        179.999 > 180 - 1.0 against the (unchanged) 180s
        search_code_handler_timeout_seconds. The batch must be rejected as
        ONE unit, so the accompanying, individually-valid
        default_handler_timeout_seconds change must NOT be persisted."""
        svc = _make_service(str(tmp_path))
        original_default = (
            svc.get_config().search_timeouts_config.default_handler_timeout_seconds
        )
        original_temporal_wait = (
            svc.get_config().search_timeouts_config.temporal_inline_wait_seconds
        )

        response = _run_update_config_section(
            "search_timeouts",
            {
                "default_handler_timeout_seconds": "45",
                "temporal_inline_wait_seconds": "179.999",
            },
            svc,
        )

        body = response.body.decode()
        assert "Failed to save" in body, (
            f"Expected the cross-field-invalid batch to be rejected, got: {body!r}"
        )
        cfg = svc.get_config().search_timeouts_config
        assert cfg.default_handler_timeout_seconds == original_default
        assert cfg.temporal_inline_wait_seconds == original_temporal_wait


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
