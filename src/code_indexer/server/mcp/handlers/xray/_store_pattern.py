"""store_xray_pattern MCP handler (Issue #1935 Part 2)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.xray_pattern_authority import (
    PatternWriter,
    may_write_scope,
)

from ._infra import _get_cidx_meta_path

from .. import _utils
from .._utils import _mcp_response

_WRITE_PERMISSION = "repository:write"


def _scope_refusal(scope: str, writer: PatternWriter) -> Optional[Dict[str, Any]]:
    """Refusal for a repository scope the caller is not granted
    (``may_write_scope``), else None. An ungranted alias gets the same
    answer as one that does not exist."""
    if may_write_scope(scope, writer):
        return None
    return {
        "success": False,
        "error": "access_denied",
        "message": "Access denied: the repository scope is not accessible.",
    }


def handle_store_xray_pattern(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the store_xray_pattern tool.

    Stores a reusable xray evaluator pattern in the cidx-meta pattern library.

    Error codes:
        auth_required            — unauthenticated or missing repository:write.
        access_denied            — scope names a repository the caller cannot access.
        global_pattern_overwrite_forbidden — non-admin replacing an __any__ pattern.
        scope_required           — scope parameter missing or empty.
        pattern_yaml_required    — pattern_yaml parameter missing or empty.
        invalid_yaml             — pattern_yaml cannot be parsed as YAML.
        missing_required_field   — required field absent from pattern YAML.
        xray_evaluator_validation_failed — evaluator code fails Rust whitelist.
        pattern_already_exists   — pattern exists and overwrite=false.
        invalid_parameter        — unknown parameter name declared.
        invalid_parameter_type   — parameter type not in allowed set.
    """
    if user is None or not user.has_permission(_WRITE_PERMISSION):
        return _mcp_response(
            {
                "success": False,
                "error": "auth_required",
                "message": f"{_WRITE_PERMISSION} permission required",
            }
        )

    scope: str = params.get("scope", "")
    pattern_yaml: str = params.get("pattern_yaml", "")
    overwrite: bool = bool(params.get("overwrite", False))

    if not scope:
        return _mcp_response(
            {"error": "scope_required", "message": "scope parameter is required"}
        )
    if not pattern_yaml:
        return _mcp_response(
            {
                "error": "pattern_yaml_required",
                "message": "pattern_yaml parameter is required",
            }
        )
    writer = PatternWriter.of(user, _utils._get_access_filtering_service())
    refusal = _scope_refusal(scope, writer)
    if refusal is not None:
        from code_indexer.server.services.xray_pattern_service import (
            record_refused_pattern_store,
        )

        record_refused_pattern_store(user.username, scope)
        return _mcp_response(refusal)

    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta = _get_cidx_meta_path()
    svc = XrayPatternService(
        cidx_meta,
        refresh_scheduler=_utils._get_app_refresh_scheduler(),
    )
    result = svc.store_xray_pattern_audited(
        scope=scope,
        pattern_yaml=pattern_yaml,
        overwrite=overwrite,
        actor=user.username,
        may_replace_global=writer.admin_role,
    )
    return _mcp_response(result)
