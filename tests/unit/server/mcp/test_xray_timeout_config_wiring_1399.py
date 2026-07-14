"""
Bug #1399 CRITICAL item 5: xray.xray_timeout_seconds is dead + hardcoded
override actively used instead.

Root cause: xray_config.xray_timeout_seconds is settable and validated via
the Web UI Config screen (ConfigService._update_xray_setting), but
mcp/handlers/xray.py never references the config object at all -- the
effective timeout (when the caller omits timeout_seconds) is either a
caller-supplied param or the hardcoded module constant
_DEFAULT_TIMEOUT_SECONDS = 120.

Fix: when timeout_override is None, read the default from
get_config_service().get_config().xray_config.xray_timeout_seconds instead
of the hardcoded _DEFAULT_TIMEOUT_SECONDS constant (fail-soft: falls back to
_DEFAULT_TIMEOUT_SECONDS on any config read failure, consistent with other
live-read helpers in this codebase, e.g. memory_governor._read_live_config).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

from .test_xray_search_handler import (
    VALID_PARAMS,
    _import_handler,
    _xray_single_repo_env,
)

# Deliberately distinct from the hardcoded _DEFAULT_TIMEOUT_SECONDS (120),
# and within the valid [_TIMEOUT_MIN, _TIMEOUT_MAX] = [10, 600] range.
_CONFIGURED_XRAY_TIMEOUT_SECONDS = 55


async def _capture_engine_kwargs_with_config(
    params: Dict[str, Any], config_service: Any
) -> Dict[str, Any]:
    """Run the xray_search handler with an injected ConfigService, capturing
    the kwargs passed to XRaySearchEngine.run() (mirrors the
    _capture_engine_kwargs helper in test_xray_search_handler_params.py, but
    also patches get_config_service so the default-timeout resolution under
    test can be observed)."""
    from code_indexer.server.auth.user_manager import UserRole
    from .test_xray_search_handler import _make_user

    user = _make_user(UserRole.NORMAL_USER)
    captured: Dict[str, Any] = {}
    with (
        patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray.get_config_service",
            return_value=config_service,
        ),
    ):
        with _xray_single_repo_env() as (_bjm, _jt, _exec, mock_loop):
            await _import_handler()(params, user)
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            with patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured.update(kw)
                or {
                    "matches": [],
                    "evaluation_errors": [],
                    "files_processed": 0,
                    "files_total": 0,
                    "elapsed_seconds": 0.0,
                },
            ):
                job_fn()
    return captured


class TestXrayDefaultTimeoutReadsConfigService:
    async def test_omitted_timeout_uses_configured_xray_timeout_seconds(self):
        """
        Bug #1399: when the caller omits timeout_seconds, the effective
        timeout passed to XRaySearchEngine.run() must come from
        ConfigService's xray_config.xray_timeout_seconds, not the hardcoded
        _DEFAULT_TIMEOUT_SECONDS=120 module constant.
        """
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value.xray_config.xray_timeout_seconds = (
            _CONFIGURED_XRAY_TIMEOUT_SECONDS
        )

        params = {**VALID_PARAMS}
        assert "timeout_seconds" not in params

        captured = await _capture_engine_kwargs_with_config(params, mock_config_service)

        assert captured.get("timeout_seconds") == _CONFIGURED_XRAY_TIMEOUT_SECONDS, (
            "Bug #1399: xray_search must read the default timeout from "
            "ConfigService.get_config().xray_config.xray_timeout_seconds when "
            f"the caller omits timeout_seconds; got {captured!r}."
        )

    async def test_explicit_timeout_override_still_wins(self):
        """An explicit timeout_seconds param must still take priority over
        the configured default (no regression to the override contract)."""
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value.xray_config.xray_timeout_seconds = (
            _CONFIGURED_XRAY_TIMEOUT_SECONDS
        )

        explicit_override = 33
        params = {**VALID_PARAMS, "timeout_seconds": explicit_override}

        captured = await _capture_engine_kwargs_with_config(params, mock_config_service)

        assert captured.get("timeout_seconds") == explicit_override

    async def test_config_read_failure_falls_back_to_hardcoded_default(self):
        """A broken ConfigService must not crash the handler -- falls back to
        the module's _DEFAULT_TIMEOUT_SECONDS constant (fail-soft)."""
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_TIMEOUT_SECONDS

        class _BrokenConfigService:
            def get_config(self):
                raise RuntimeError("DB unavailable")

        params = {**VALID_PARAMS}
        captured = await _capture_engine_kwargs_with_config(
            params, _BrokenConfigService()
        )

        assert captured.get("timeout_seconds") == _DEFAULT_TIMEOUT_SECONDS
