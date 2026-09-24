"""store_xray_pattern MCP handler (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray.py -- pure relocation, zero
behaviour change.
"""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.auth.user_manager import User

from ._infra import _get_cidx_meta_path

from .. import _utils
from .._utils import _mcp_response


def handle_store_xray_pattern(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the store_xray_pattern tool.

    Stores a reusable xray evaluator pattern in the cidx-meta pattern library.

    Error codes:
        auth_required            — unauthenticated or missing query_repos.
        scope_required           — scope parameter missing or empty.
        pattern_yaml_required    — pattern_yaml parameter missing or empty.
        invalid_yaml             — pattern_yaml cannot be parsed as YAML.
        missing_required_field   — required field absent from pattern YAML.
        xray_evaluator_validation_failed — evaluator code fails Rust whitelist.
        pattern_already_exists   — pattern exists and overwrite=false.
        invalid_parameter        — unknown parameter name declared.
        invalid_parameter_type   — parameter type not in allowed set.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

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

    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta = _get_cidx_meta_path()
    svc = XrayPatternService(
        cidx_meta,
        refresh_scheduler=_utils._get_app_refresh_scheduler(),
    )
    result = svc.store_xray_pattern(
        scope=scope,
        pattern_yaml=pattern_yaml,
        overwrite=overwrite,
    )
    return _mcp_response(result)
