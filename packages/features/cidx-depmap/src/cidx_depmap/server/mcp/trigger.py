"""MCP tool ``trigger_dependency_analysis`` (Story #195): start a full or
delta dependency-map analysis as a background job.

Contributed by the cidx-depmap feature (``tools()``, modular phase 4g); it
was in the core's admin handlers before. The core checks the tool's
permission; the handler answers a non-admin role first, as it did.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from cidx_depmap._platform import server_platform
from cidx_depmap.server import runtime

logger = logging.getLogger(__name__)


def _mcp_response(data: Dict[str, Any]) -> Dict[str, Any]:
    return server_platform().mcp_response(data)


def format_error_log(error_code: str, message: str, **context: Any) -> str:
    return server_platform().format_error_log(error_code, message, **context)


def handle_trigger_dependency_analysis(
    args: Dict[str, Any], user: Any
) -> Dict[str, Any]:
    """
    Trigger dependency map analysis manually (Story #195).

    Args:
        args: Tool arguments with optional mode ("full" or "delta")
        user: The authenticated user making the request

    Returns:
        MCP response with job_id, mode, and status
    """
    denied = server_platform().admin_role_required_response(user)
    if denied is not None:
        return denied
    from cidx_depmap._platform import get_config_service

    try:
        # AC4: Default mode is delta
        mode = args.get("mode", "delta") or "delta"

        # AC8: Validate mode parameter
        if mode not in ["full", "delta"]:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Invalid mode '{mode}'. Must be 'full' or 'delta'.",
                    "job_id": None,
                }
            )

        # AC6: Check if feature is enabled
        _server_config = get_config_service().get_config()
        _ci_config = (
            runtime.DepMapClaudeConfig(_server_config.claude_integration_config)
            if _server_config
            else None
        )
        if not _ci_config or not getattr(_ci_config, "dependency_map_enabled", False):
            return _mcp_response(
                {
                    "success": False,
                    "error": "Dependency map analysis is disabled",
                    "job_id": None,
                }
            )

        # AC5: Check if analysis is already running
        dependency_map_service = runtime.dependency_map_service()
        if not dependency_map_service:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Dependency map service not available",
                    "job_id": None,
                }
            )

        # Delegated content backend: refused before any sentinel is claimed
        # (the analysis agent would read an empty repository tree).
        from cidx_depmap.server.dep_map_delegated import analysis_refusal

        _refusal = analysis_refusal()
        if _refusal is not None:
            _refused: Dict[str, Any] = _mcp_response(
                {"success": False, "error": _refusal, "job_id": None, "mode": mode}
            )
            return _refused

        if not dependency_map_service.is_available():
            # Story #1035 AC6: read active sentinel to surface job_id in error envelope
            _active_job_id: str = "unknown"
            _sentinel_dir_pf = (
                dependency_map_service.get_sentinel_dir()
                if hasattr(dependency_map_service, "get_sentinel_dir")
                else None
            )
            if _sentinel_dir_pf is not None:
                from cidx_depmap._platform import (
                    SharedJobSentinel,
                )
                from cidx_depmap.server.dependency_map_service import (
                    ANALYSIS_STALE_TIMEOUT_SECONDS as _ANALYSIS_STALE_TIMEOUT_SECONDS,
                )

                _sentinel_pf = SharedJobSentinel(
                    sentinel_dir=_sentinel_dir_pf,
                    stale_timeout_seconds=_ANALYSIS_STALE_TIMEOUT_SECONDS,
                )
                _active_info = _sentinel_pf.read_active("analysis")
                if _active_info is not None:
                    _active_job_id = _active_info.job_id
            return _mcp_response(
                {
                    "success": False,
                    "error": "already in progress",
                    "job_id": _active_job_id,
                    "mode": mode,
                }
            )

        # AC5 (Story #919): dry-run graph-repair mode — synchronous, no background job
        dry_run_raw = args.get("dry_run_graph_only", False)
        if dry_run_raw is not False and not isinstance(dry_run_raw, bool):
            return _mcp_response(
                {
                    "success": False,
                    "error": "dry_run_graph_only must be a boolean",
                    "job_id": None,
                }
            )
        dry_run_graph_only: bool = bool(dry_run_raw)
        if dry_run_graph_only:
            dry_run_report = dependency_map_service.run_graph_repair_dry_run()
            return _mcp_response(
                {
                    "success": True,
                    "job_id": None,
                    "mode": mode,
                    "status": "completed",
                    "message": "Dry-run graph-channel repair report",
                    "graph_repair_dry_run_report": dry_run_report,
                }
            )

        # Generate job ID
        job_id = f"dep-map-{mode}-{uuid.uuid4().hex[:8]}-{int(datetime.now(timezone.utc).timestamp())}"

        # Story #1035 AC13: synchronous sentinel claim before spawning thread
        _pre_claimed = False
        _sentinel_obj = None
        _sentinel_dir_sc = (
            dependency_map_service.get_sentinel_dir()
            if hasattr(dependency_map_service, "get_sentinel_dir")
            else None
        )
        if isinstance(_sentinel_dir_sc, (str, Path)):
            from cidx_depmap._platform import (
                SharedJobSentinel as _SharedJobSentinel,
            )
            from cidx_depmap.server.dependency_map_service import (
                ANALYSIS_STALE_TIMEOUT_SECONDS as _ANALYSIS_STALE_TIMEOUT_SECONDS_SC,
            )

            _sentinel_obj = _SharedJobSentinel(
                sentinel_dir=Path(_sentinel_dir_sc),
                stale_timeout_seconds=_ANALYSIS_STALE_TIMEOUT_SECONDS_SC,
            )
            _node_id_sc = (
                dependency_map_service._get_node_id()
                if hasattr(dependency_map_service, "_get_node_id")
                else "unknown"
            )
            _claim = _sentinel_obj.try_claim("analysis", job_id, _node_id_sc)
            if not _claim.success:
                _conflict_job_id = (
                    _claim.active.job_id if _claim.active is not None else "unknown"
                )
                return _mcp_response(
                    {
                        "success": False,
                        "error": "already in progress",
                        "job_id": _conflict_job_id,
                        "mode": mode,
                    }
                )
            _pre_claimed = True

        # AC2/AC3: Spawn background thread for analysis
        def run_analysis_job() -> None:
            """Background job to run dependency map analysis."""
            from cidx_depmap._platform import (
                AnalysisAlreadyRunningError as _BgAnalysisAlreadyRunningError,
            )
            from cidx_depmap._platform import (
                DuplicateJobError as _BgDuplicateJobError,
            )

            try:
                if mode == "full":
                    dependency_map_service.run_full_analysis(
                        job_id=job_id, pre_claimed=_pre_claimed
                    )
                else:
                    dependency_map_service.run_delta_analysis(
                        job_id=job_id, pre_claimed=_pre_claimed
                    )
            except (_BgDuplicateJobError, _BgAnalysisAlreadyRunningError) as e:
                _active_dup_id = (
                    getattr(e, "existing_job_id", None)
                    or getattr(e, "active_job_id", None)
                    or "unknown"
                )
                logger.info(
                    "MCP dep-map trigger rejected: already in progress, job_id=%s",
                    _active_dup_id,
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "DEPMAP-TRIGGER-001",
                        f"Background dependency map analysis failed: {e}",
                    )
                )

        # Start background daemon thread
        thread = threading.Thread(target=run_analysis_job, daemon=True)
        try:
            thread.start()
        except Exception:
            # N1: release sentinel on thread-spawn failure to avoid leak
            if _pre_claimed and _sentinel_obj is not None:
                _sentinel_obj.release("analysis", expected_job_id=job_id)
            raise

        # AC2/AC3: Return job_id immediately
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "mode": mode,
                "status": "queued",
                "message": f"Dependency map {mode} analysis started",
            }
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "DEPMAP-TRIGGER-002",
                f"Error triggering dependency map analysis: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def _register(registry: Dict[str, Any]) -> None:
    """Register the tool's handler."""
    registry["trigger_dependency_analysis"] = handle_trigger_dependency_analysis
