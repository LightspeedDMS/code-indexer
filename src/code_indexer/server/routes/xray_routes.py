"""REST route POST /api/xray/search (Story #974).

Thin shim: validate inputs, pre-flight check evaluator, submit background job.
Mirrors the MCP xray_search handler (handlers/xray.py) — same validation logic,
HTTP-shaped response. Returns HTTP 202 with {"job_id": "<uuid>"}; clients poll
the existing GET /api/jobs/{job_id} endpoint for progress and final results.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional, Union, cast

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User
from code_indexer.xray.sandbox import validate_rust_evaluator

logger = logging.getLogger(__name__)

# Timeout range enforced here — matches the MCP handler constants.
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600
_DEFAULT_TIMEOUT_SECONDS = 120

router = APIRouter(prefix="/api/xray", tags=["xray"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class XRaySearchRequest(BaseModel):
    """Pydantic request body for POST /api/xray/search."""

    repository_alias: str
    driver_regex: str
    evaluator_code: str
    search_target: str  # validated manually; Literal requires py3.8+ typing compat
    include_patterns: List[str] = Field(default_factory=list)
    exclude_patterns: List[str] = Field(default_factory=list)
    timeout_seconds: Optional[int] = None
    max_files: Optional[int] = None


class XRaySearchResponse(BaseModel):
    """HTTP 202 response body for POST /api/xray/search."""

    job_id: str


# ---------------------------------------------------------------------------
# Helpers — extracted for easy mocking in tests
# ---------------------------------------------------------------------------


def _resolve_repo_path(alias: str) -> Optional[str]:
    """Resolve a global repo alias to its versioned snapshot path.

    Returns None when the alias is unknown.
    """
    from code_indexer.server.mcp.handlers.repos import _resolve_golden_repo_path

    return cast(Optional[str], _resolve_golden_repo_path(alias))


def _get_background_job_manager() -> Any:
    """Return the live BackgroundJobManager from the app module."""
    from code_indexer.server.mcp.handlers import _utils

    return _utils.app_module.background_job_manager


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post(
    "/search",
    response_model=XRaySearchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def xray_search(
    body: XRaySearchRequest,
    user: User = Depends(get_current_user),
) -> XRaySearchResponse:
    """Submit an X-Ray AST search job and return its job_id.

    1. Permission check (query_repos).
    2. Field validation (search_target, timeout_seconds range, max_files).
    3. Repository alias resolution.
    4. Pre-flight evaluator validation via PythonEvaluatorSandbox.
    5. Job submission via BackgroundJobManager.
    6. Return HTTP 202 with {job_id}.

    Error codes:
        auth_required              — missing query_repos permission (403)
        invalid_search_target      — search_target not 'content' or 'filename' (422)
        timeout_out_of_range       — timeout_seconds outside [10, 600] (422)
        max_files_out_of_range     — max_files provided but < 1 (422)
        repository_not_found       — alias cannot be resolved (404)
        xray_extras_not_installed  — tree-sitter extras not available (503)
        xray_evaluator_validation_failed — evaluator AST whitelist violation (422)
    """
    # ------------------------------------------------------------------
    # 1. Permission check
    # ------------------------------------------------------------------
    if not user.has_permission("query_repos"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "auth_required",
                "detail": "query_repos permission required",
            },
        )

    # ------------------------------------------------------------------
    # 2. Field validation
    # ------------------------------------------------------------------
    if body.search_target not in ("content", "filename"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "invalid_search_target",
                "detail": (
                    f"search_target must be 'content' or 'filename', "
                    f"got {body.search_target!r}"
                ),
            },
        )

    if body.max_files is not None and body.max_files < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "max_files_out_of_range",
                "detail": "max_files must be >= 1",
            },
        )

    effective_timeout: int = (
        body.timeout_seconds
        if body.timeout_seconds is not None
        else _DEFAULT_TIMEOUT_SECONDS
    )
    if not (_TIMEOUT_MIN <= effective_timeout <= _TIMEOUT_MAX):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "timeout_out_of_range",
                "detail": (
                    f"timeout_seconds must be between {_TIMEOUT_MIN} and "
                    f"{_TIMEOUT_MAX}, got {effective_timeout}"
                ),
            },
        )

    # ------------------------------------------------------------------
    # 3. Repository alias resolution
    # ------------------------------------------------------------------
    repo_path_str = _resolve_repo_path(body.repository_alias)
    if repo_path_str is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "repository_not_found",
                "detail": f"Repository alias {body.repository_alias!r} not found",
            },
        )

    # ------------------------------------------------------------------
    # 4. Pre-flight evaluator validation
    # ------------------------------------------------------------------
    validation = validate_rust_evaluator(body.evaluator_code)
    if not validation.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "xray_evaluator_validation_failed",
                "detail": validation.reason,
            },
        )

    # ------------------------------------------------------------------
    # 5. Submit background job
    # ------------------------------------------------------------------
    repo_path = Path(repo_path_str)
    include_patterns = list(body.include_patterns)
    exclude_patterns = list(body.exclude_patterns)
    max_files = body.max_files

    def job_fn(progress_callback):  # type: ignore[no-untyped-def]
        from code_indexer.xray.search_engine import XRaySearchEngine as _Engine

        return _Engine().run(
            repo_path=repo_path,
            driver_regex=body.driver_regex,
            evaluator_code=body.evaluator_code,
            search_target=body.search_target,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            timeout_seconds=effective_timeout,
            progress_callback=progress_callback,
            max_files=max_files,
        )

    bjm = _get_background_job_manager()
    job_id: str = bjm.submit_job(
        operation_type="xray_search",
        func=job_fn,
        submitter_username=user.username,
        repo_alias=body.repository_alias,
    )

    logger.info(
        "xray_search REST job submitted",
        extra={
            "user_id": user.username,
            "repo_alias": body.repository_alias,
            "driver_regex": body.driver_regex[:100],
            "search_target": body.search_target,
        },
    )

    return XRaySearchResponse(job_id=job_id)


# ---------------------------------------------------------------------------
# Batch request / response models (Story #1055)
# ---------------------------------------------------------------------------


class XRayBatchScanBundle(BaseModel):
    """One scan bundle for xray_search_batch."""

    driver_regex: str
    evaluator_code: Optional[str] = None
    pattern_name: Optional[str] = None
    pattern_params: Optional[Any] = None
    search_target: str = "content"
    case_sensitive: bool = True
    multiline: bool = False
    pcre2: bool = False


class XRayBatchSearchRequest(BaseModel):
    """Pydantic request body for POST /api/xray/search/batch."""

    repository_alias: Any  # str | list[str] -- validated by handler
    scans: List[XRayBatchScanBundle]
    max_results: Optional[int] = None
    timeout_seconds: Optional[int] = None
    await_seconds: Optional[float] = None


class XRayBatchSearchResponse(BaseModel):
    """HTTP 202 response body for POST /api/xray/search/batch."""

    job_id: str


# ---------------------------------------------------------------------------
# Batch route handler (Story #1055)
# ---------------------------------------------------------------------------

# Map synchronous MCP error codes to HTTP status codes.
_BATCH_ERROR_HTTP_STATUS = {
    "auth_required": status.HTTP_403_FORBIDDEN,
    "alias_required": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "scans_required": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "too_many_repositories": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "too_many_scans": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "timeout_out_of_range": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "await_seconds_out_of_range": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "driver_regex_required": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "mutually_exclusive_params": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "xray_evaluator_validation_failed": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "no_repositories_resolved": status.HTTP_404_NOT_FOUND,
}


@router.post(
    "/search/batch",
    response_model=XRayBatchSearchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def xray_search_batch(
    body: XRayBatchSearchRequest,
    user: User = Depends(get_current_user),
) -> Union[XRayBatchSearchResponse, JSONResponse]:
    """Submit a cross-repo multi-expression X-Ray batch job.

    Delegates to handle_xray_search_batch (same logic as the MCP tool).
    Returns HTTP 202 with {"job_id": "<uuid>"}; poll GET /api/jobs/{job_id}.

    Error codes follow the NEW batch contract (not the legacy xray_search REST
    shape which uses driver_regex, requires evaluator_code, and lacks pattern_name).
    """
    import json as _json

    from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

    # Convert Pydantic model to the params dict expected by the MCP handler.
    scans_list = [
        {
            "driver_regex": s.driver_regex,
            "evaluator_code": s.evaluator_code,
            "pattern_name": s.pattern_name,
            "pattern_params": s.pattern_params,
            "search_target": s.search_target,
            "case_sensitive": s.case_sensitive,
            "multiline": s.multiline,
            "pcre2": s.pcre2,
        }
        for s in body.scans
    ]

    params: dict = {
        "repository_alias": body.repository_alias,
        "scans": scans_list,
    }
    if body.max_results is not None:
        params["max_results"] = body.max_results
    if body.timeout_seconds is not None:
        params["timeout_seconds"] = body.timeout_seconds
    if body.await_seconds is not None:
        params["await_seconds"] = body.await_seconds

    mcp_resp = handle_xray_search_batch(params, user)

    # Unwrap MCP envelope.
    try:
        resp_data = _json.loads(mcp_resp["content"][0]["text"])
    except (KeyError, IndexError, _json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "internal_error", "detail": str(exc)},
        ) from exc

    if "error" in resp_data:
        error_code = resp_data["error"]
        http_status = _BATCH_ERROR_HTTP_STATUS.get(
            error_code, status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=http_status,
            detail=resp_data,
        )

    # Inline-result parity: when await_seconds > 0 and the job completed
    # before the poll window expired, the MCP handler returns the full result
    # dict (matches, errors, …) instead of {"job_id": …}.  In that case we
    # return HTTP 200 with the complete result so REST callers get the same
    # data without needing to poll.
    if "job_id" not in resp_data:
        return JSONResponse(content=resp_data, status_code=status.HTTP_200_OK)

    return XRayBatchSearchResponse(job_id=resp_data["job_id"])
