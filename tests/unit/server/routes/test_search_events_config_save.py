"""Unit tests for Finding H1: search_event_log section must be in
_VALID_CONFIG_SECTIONS and validated by _validate_config_section (Story #1159).

Before the fix: POST /admin/config/search_event_log returned HTTP 400 "Invalid
section: search_event_log" because "search_event_log" was missing from
_VALID_CONFIG_SECTIONS, making the Web UI save dead for that section.

These tests exercise the two functions directly so no web session/CSRF wiring
is needed, keeping the tests fast and dependency-free.
"""

# ---------------------------------------------------------------------------
# H1.1 - section membership
# ---------------------------------------------------------------------------


class TestSearchEventLogSectionMembership:
    """search_event_log must be present in _VALID_CONFIG_SECTIONS."""

    def test_search_event_log_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "search_event_log" in _VALID_CONFIG_SECTIONS, (
            "search_event_log must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/search_event_log would otherwise always return "
            "HTTP 400 'Invalid section: search_event_log'."
        )


# ---------------------------------------------------------------------------
# H1.2 - validation logic
# ---------------------------------------------------------------------------


class TestValidateConfigSectionSearchEventLog:
    """_validate_config_section("search_event_log", ...) enforces [1, 3650]."""

    def _validate(self, data: dict):
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("search_event_log", data)

    def test_valid_retention_days_returns_none(self) -> None:
        assert self._validate({"search_event_log_retention_days": 30}) is None

    def test_boundary_value_1_accepted(self) -> None:
        assert self._validate({"search_event_log_retention_days": 1}) is None

    def test_boundary_value_3650_accepted(self) -> None:
        assert self._validate({"search_event_log_retention_days": 3650}) is None

    def test_string_int_accepted(self) -> None:
        # Form POST sends strings; must coerce cleanly.
        assert self._validate({"search_event_log_retention_days": "365"}) is None

    def test_missing_field_returns_none(self) -> None:
        # Partial save: other keys only — must not error.
        assert self._validate({}) is None

    def test_zero_days_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": 0})
        assert error is not None
        assert "1" in error and "3650" in error

    def test_negative_days_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": -1})
        assert error is not None

    def test_exceeding_3650_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": 3651})
        assert error is not None
        assert "3650" in error

    def test_non_integer_string_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": "abc"})
        assert error is not None
        assert "integer" in error.lower()

    def test_float_string_rejected(self) -> None:
        # "30.5" cannot be int()'d cleanly.
        error = self._validate({"search_event_log_retention_days": "30.5"})
        assert error is not None


# ---------------------------------------------------------------------------
# Bug #1180 - Saving the search_event_log section with BOTH fields must not
# raise ValueError.
#
# Root cause: update_config_section (routes.py) calls
#   config_service.update_setting(section, key, value)
# for every posted key using the URL section as category.  When
# section="search_event_log" and key="export_retention_days", this routes
# export_retention_days to _update_search_event_log_setting which raises
#   ValueError("Unknown search_event_log setting: export_retention_days")
#
# Fix: update_config_section must route export_retention_days to
# category="export" instead of category="search_event_log".
#
# Tests call config_service.update_setting directly — exactly what the route
# handler does at line 8847 — so they exercise real production code without
# needing an HTTP client.
# ---------------------------------------------------------------------------


def _make_service_1180(tmp_dir: str):
    """Return a ConfigService backed by a real SQLite DB."""
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    return ConfigService(config_manager=ServerConfigManager(server_dir_path=tmp_dir))


class TestSearchEventLogSaveBug1180:
    """Bug #1180: export_retention_days posted to /admin/config/search_event_log
    must be routed to category='export', not category='search_event_log'.
    """

    def test_wrong_category_raises_value_error(self, tmp_path) -> None:
        """Calling update_setting('search_event_log', 'export_retention_days', ...)
        raises ValueError.  This is exactly what the buggy route code does.

        RED phase: proves the defect exists in the production config service so
        the fix must change the route, not silently swallow the error.
        """
        import pytest

        svc = _make_service_1180(str(tmp_path))
        with pytest.raises(ValueError, match="Unknown search_event_log setting"):
            svc.update_setting(
                "search_event_log", "export_retention_days", "60", skip_validation=True
            )

    def test_correct_category_saves_without_error(self, tmp_path) -> None:
        """Calling update_setting('export', 'export_retention_days', ...) succeeds
        and persists the value.  This is what the fixed route must do.
        """
        svc = _make_service_1180(str(tmp_path))
        svc.update_setting(
            "export", "export_retention_days", "60", skip_validation=True
        )
        assert svc.get_config().export_retention_days == 60

    def test_validate_accepts_both_fields(self) -> None:
        """_validate_config_section('search_event_log', ...) must accept both
        search_event_log_retention_days and export_retention_days without error.
        Validation runs before save; rejecting export_retention_days here would
        block saves before they even reach update_setting.
        """
        from code_indexer.server.web.routes import _validate_config_section

        error = _validate_config_section(
            "search_event_log",
            {"search_event_log_retention_days": "180", "export_retention_days": "60"},
        )
        assert error is None, f"_validate_config_section rejected valid data: {error!r}"


# ---------------------------------------------------------------------------
# Bug #1180 - Regression: exercises the ACTUAL update_config_section handler
#
# The three tests above call config_service.update_setting() directly.  They
# pass even when the fix in update_config_section is removed because they
# never drive the override-dict code path (lines 8853-8858 of routes.py).
#
# This class invokes the real async handler coroutine directly so that
# removing _section_key_category_overrides causes the test to fail:
# update_setting('search_event_log', 'export_retention_days', ...) raises
# ValueError which the handler catches and returns an error response.  The
# test asserts the response does NOT indicate an error and that both values
# actually persisted in the DB.
# ---------------------------------------------------------------------------


class TestUpdateConfigSectionHandlerDirect:
    """Regression for Bug #1180: drives the real update_config_section
    coroutine with both search_event_log fields and asserts both persist.

    Patched only:
      - _require_admin_session  -> fake SessionData (bypasses cookie/JWT)
      - validate_login_csrf_token -> True (bypasses CSRF)
      - get_config_service -> real ConfigService on tmp SQLite
      - _create_config_page_response -> minimal HTMLResponse stub
        (avoids Jinja2/template/token-manager wiring irrelevant to this test)
    """

    def _build_fake_request(self, form_data: dict):
        """Build a minimal async-compatible Request stub with given form data."""
        from unittest.mock import AsyncMock, MagicMock
        from starlette.datastructures import ImmutableMultiDict

        req = MagicMock()
        req.session = {}

        # form() is an async method returning FormData-like mapping
        items = list(form_data.items())
        multi = ImmutableMultiDict(items)
        req.form = AsyncMock(return_value=multi)
        return req

    def test_both_fields_save_via_real_handler(self, tmp_path) -> None:
        """Invoke the real update_config_section handler with both
        search_event_log_retention_days and export_retention_days.

        Without the fix: update_setting raises ValueError for
        export_retention_days; the handler catches it and returns an
        error response; the DB shows export_retention_days unchanged.

        With the fix: both values persist; the response carries a success
        message.  The test asserts both values are in the DB.
        """
        import asyncio
        from unittest.mock import MagicMock, patch

        from fastapi.responses import HTMLResponse

        from code_indexer.server.web.routes import update_config_section

        svc = _make_service_1180(str(tmp_path))

        # Minimal SessionData-like object (handler only reads .username)
        fake_session = MagicMock()
        fake_session.username = "admin"
        fake_session.role = "admin"

        req = self._build_fake_request(
            {
                "search_event_log_retention_days": "180",
                "export_retention_days": "60",
            }
        )

        def _fake_page_response(request, session, **kwargs):
            # Capture any error_message so the test can inspect it
            body = kwargs.get("error_message") or kwargs.get("success_message") or "ok"
            return HTMLResponse(content=body)

        with (
            patch(
                "code_indexer.server.web.routes._require_admin_session",
                return_value=fake_session,
            ),
            patch(
                "code_indexer.server.web.routes.validate_login_csrf_token",
                return_value=True,
            ),
            patch(
                # update_config_section has a LOCAL import:
                #   from ..services.config_service import get_config_service
                # at line 8762.  This shadows the module-level import, so we
                # must patch the source module, not the routes namespace.
                "code_indexer.server.services.config_service.get_config_service",
                return_value=svc,
            ),
            patch(
                "code_indexer.server.web.routes._create_config_page_response",
                side_effect=_fake_page_response,
            ),
        ):
            response = asyncio.run(
                update_config_section(
                    request=req,
                    section="search_event_log",
                    csrf_token="dummy-token",
                )
            )

        # The response body must NOT contain a failure message.
        # When the fix is absent, the handler catches ValueError and returns
        #   "Failed to save configuration: Unknown search_event_log setting: ..."
        body = response.body.decode()
        assert "Failed to save" not in body, (
            f"Handler returned an error response — fix may be missing: {body!r}"
        )

        # Both values must have persisted in the real SQLite DB.
        cfg = svc.get_config()
        assert cfg.search_event_log_retention_days == 180, (
            f"Expected search_event_log_retention_days=180, "
            f"got {cfg.search_event_log_retention_days}"
        )
        assert cfg.export_retention_days == 60, (
            f"Expected export_retention_days=60, got {cfg.export_retention_days}"
        )
