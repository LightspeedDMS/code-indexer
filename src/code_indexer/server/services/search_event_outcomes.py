"""Outcome of a search, decided once per result shape (Story #2117).

Every search door records the searches it ran through
`search_event_recording`; this module decides, from the result the door
already has, whether the search succeeded or failed and, for a failure, its
error class. An error class is a short neutral token (e.g. `timeout`,
`repository_failed`, or an exception class name) -- never an error message,
path, query text or user data.

Classifiers return None for a successful search, else the error class.

A request rejected before its repository resolved (an unknown repository, a
parameter the door refuses, a service the search needs that is not wired) is
not a search and is not recorded: `is_rejected_payload` and
`failure_error_class` decide that here, for every door.
"""

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# An error class is an identifier-like token of at most 64 characters.
_ERROR_CLASS_MAX_CHARS = 64
_ERROR_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,%d}" % (_ERROR_CLASS_MAX_CHARS - 1))
_NON_TOKEN_CHARACTER = re.compile(r"[^A-Za-z0-9_]")

# Bug #1804's completeness marker: every embedding provider was unavailable,
# so the search answered success with no results without ever running.
_PROVIDERS_UNAVAILABLE = "providers_unavailable"

# A handled error payload that names no error code of its own.
SEARCH_ERROR = "search_error"

# A multi-repository search's repository that failed or was skipped.
REPOSITORY_FAILED = "repository_failed"

# An omni search that searched no repository.
NO_REPOSITORY_SEARCHED = "no_repository_searched"

# The file_path the X-Ray engine gives an evaluation error about the whole
# run rather than one file (compile error, xray-cli failure, the synthetic
# EvaluatorTimeout of a run that timed out with no matches).
_WHOLE_RUN_FILE_PATH = ""

# Error class of an X-Ray run whose whole-run evaluation error has this type.
_XRAY_WHOLE_RUN_CLASSES = {
    "CompileError": "compile_error",
    "EvaluatorTimeout": "timeout",
    "UnparsedFiles": "unparsed_files",
    "UnsupportedLanguage": "unsupported_language",
    "XRayCliError": "xray_cli_error",
}

# The list a SCIP response carries its results in, by operation:
# definition/references/dependencies/dependents "results", impact
# "affected_symbols", callchain "chains", context "files".
_SCIP_RESULT_LISTS = ("results", "affected_symbols", "chains", "files")


# Error codes of a handled-error payload that rejected the request before its
# repository resolved; the doors put them on such payloads.
REPOSITORY_NOT_FOUND_CODE = "REPOSITORY_NOT_FOUND"
SERVICE_UNAVAILABLE_CODE = "SERVICE_UNAVAILABLE"
REJECTION_ERROR_CODES = frozenset(
    {
        REPOSITORY_NOT_FOUND_CODE,
        SERVICE_UNAVAILABLE_CODE,
        # TemporalAliasRejectedError's codes.
        "TEMPORAL_ALIAS_REQUIRED",
        "TEMPORAL_SINGLE_REPO_REQUIRED",
    }
)


def is_rejected_payload(payload: Dict[str, Any]) -> bool:
    """Whether a handled-error payload rejected the request before its
    repository resolved (nothing is recorded)."""
    return (
        payload.get("success") is not True
        and payload.get("error_code") in REJECTION_ERROR_CODES
    )


def _is_rejection_error(exc: BaseException) -> bool:
    from code_indexer.server.query.semantic_query_manager import SearchRequestError
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )
    from code_indexer.services.temporal.temporal_worker_input_adapters import (
        TemporalAliasRejectedError,
    )

    return isinstance(
        exc,
        (
            SearchRequestError,
            TemporalAliasRejectedError,
            AccessFilteringServiceUnavailableError,
        ),
    )


def failure_error_class(exc: BaseException) -> Optional[str]:
    """The error class a search that raised `exc` records, or None when `exc`
    rejected the request before its repository resolved (nothing recorded).

    Rejections: SearchRequestError (input validation, unknown repository),
    TemporalAliasRejectedError, an unavailable access service, and an
    HTTPException the door raised itself (no cause) or raised translating
    one of those. An HTTPException translating any other error records that
    error's class; so does a bare `Exception` wrapper, which names no class
    of its own (e.g. `Exception("Query timed out: ...") from e`).
    """
    from fastapi import HTTPException

    error: BaseException = exc
    if isinstance(exc, HTTPException):
        cause = exc.__cause__ or exc.__context__
        if cause is None:
            return None
        error = cause
    wrapped = error.__cause__ or error.__context__
    if type(error) is Exception and wrapped is not None:
        error = wrapped
    if _is_rejection_error(error):
        return None
    return error_class_of(error)


def repository_resolved(exc: BaseException) -> bool:
    """Whether a multi-repository search's per-repository error `exc` came
    from a repository that resolved and then failed (Bug #2111:
    `repository_failed`), rather than an alias that never resolved or a
    rejected request (nothing recorded)."""
    from code_indexer.server.multi.models import UnresolvedRepositoryError

    if isinstance(exc, UnresolvedRepositoryError):
        return False
    return failure_error_class(exc) is not None


def http_response_failure_class(
    status_code: int, body: Dict[str, Any]
) -> Optional[str]:
    """The error class of a search a REST door answered with a non-success
    `status_code` and JSON `body` without raising, or None for a rejection.

    Only the body decides: a rejection carries a rejection error code
    (REJECTION_ERROR_CODES). A status code alone never makes a rejection --
    a 4xx or 503 answered after the search started is a failure.
    """
    if is_rejected_payload({**body, "success": False}):
        return None
    return _payload_error_class(body)


def is_valid_error_class(error_class: str) -> bool:
    """Whether `error_class` is a neutral token (never a message)."""
    return _ERROR_CLASS.fullmatch(error_class) is not None


def error_class_of(exc: BaseException) -> str:
    """The error class of a search that raised: its exception class name as a
    valid token (characters outside [A-Za-z0-9_] replaced, leading
    underscores and digits dropped, cut to 64 characters), so the row is
    never refused for its class name."""
    token = _NON_TOKEN_CHARACTER.sub("_", type(exc).__name__).lstrip("_0123456789")
    return token[:_ERROR_CLASS_MAX_CHARS] or "error"


def _payload_error_class(payload: Dict[str, Any]) -> str:
    """A handled-error payload's class: its own error code when it carries a
    neutral one (lower-cased), else `search_error`. Never its message."""
    code = payload.get("error_code")
    if isinstance(code, str) and is_valid_error_class(code):
        return code.lower()
    return SEARCH_ERROR


def search_payload_outcome(payload: Dict[str, Any]) -> Optional[str]:
    """Outcome of a search_code response payload.

    Failures: `success` not true (a handled error), every embedding provider
    unavailable (Bug #1804: success true with completeness
    "providers_unavailable", the search never ran), and an omni search that
    searched no repository (success true, total_repos_searched 0).
    """
    if payload.get("success") is not True:
        return _payload_error_class(payload)
    results = payload.get("results")
    if not isinstance(results, dict):
        return None
    metadata = results.get("query_metadata")
    if isinstance(metadata, dict) and (
        metadata.get("completeness") == _PROVIDERS_UNAVAILABLE
    ):
        return _PROVIDERS_UNAVAILABLE
    if "total_repos_searched" in results and not results["total_repos_searched"]:
        return NO_REPOSITORY_SEARCHED
    return None


def search_payload_result_count(payload: Dict[str, Any]) -> int:
    """Number of results in a search_code response payload.

    `results` is a list (global path), a dict wrapping a `results` list
    (activated-repo and temporal paths, flat omni), or a grouped omni dict
    carrying `total_results`.
    """
    results = payload.get("results")
    if isinstance(results, dict):
        if "results_by_repo" in results:
            return int(results.get("total_results") or 0)
        results = results.get("results")
    return len(results) if isinstance(results, list) else 0


def payload_repository_counts(payload: Dict[str, Any]) -> Dict[str, int]:
    """Results per repository in a multi-repository search_code payload
    (Bug #2111): omni grouped (`results_by_repo` counts) or flat
    (`source_repo` of each), or a search over every activated repository
    (`repository_alias` of each; `source_repo` there names a composite's
    component)."""
    results = payload.get("results")
    if not isinstance(results, dict):
        return {}
    grouped = results.get("results_by_repo")
    if isinstance(grouped, dict):
        return {alias: int(group.get("count") or 0) for alias, group in grouped.items()}
    key = "source_repo" if "total_repos_searched" in results else "repository_alias"
    return results_repository_counts(results.get("results") or [], key)


def results_repository_counts(results: Iterable[Any], key: str) -> Dict[str, int]:
    """Results per repository in a result list, by each result's `key`."""
    counts: Counter[str] = Counter(
        str(r[key]) for r in results if isinstance(r, dict) and r.get(key)
    )
    return dict(counts)


def _whole_run_failure(error: Dict[str, Any]) -> bool:
    """An evaluation error that names no file is about the whole run: the
    engine's marker for a compile error, an xray-cli or output failure, and
    its synthetic EvaluatorTimeout (search_engine.py, Bug #1827). A per-file
    error (e.g. one file's 5 s sandbox timeout) names its file."""
    return error.get("file_path") == _WHOLE_RUN_FILE_PATH


def _xray_whole_run_class(error: Dict[str, Any]) -> str:
    return _XRAY_WHOLE_RUN_CLASSES.get(
        str(error.get("error_type")), "evaluation_failed"
    )


def _xray_failed_cells(result: Dict[str, Any]) -> Set[Tuple[Any, Any]]:
    """(repository_alias, scan_index) of every batch cell that did not run:
    a cell-level error, or a whole-run evaluation error of that cell."""
    return {
        (e.get("repository_alias"), e.get("scan_index"))
        for e in result.get("errors") or []
        if e.get("error_level") == "cell"
    } | {
        (e.get("repository_alias"), e.get("scan_index"))
        for e in result.get("evaluation_errors") or []
        if _whole_run_failure(e)
    }


def xray_result_outcome(result: Dict[str, Any]) -> Optional[str]:
    """Outcome of an X-Ray engine or batch result.

    Failures: a Phase 1 (ripgrep) failure, a cancelled batch, a timeout with
    no matches, a single run with no matches whose evaluator never ran (a
    whole-run evaluation error: compile, xray-cli, timeout), and a batch in
    which no cell ran. A partial result with matches (timeout, file cap,
    some failed cells, per-file errors) is a success.
    """
    if result.get("phase1_failed"):
        return "phase1_failed"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("matches"):
        return None
    if result.get("timeout"):
        return "timeout"
    if "total_cells" not in result:
        # A single engine run (not a batch).
        whole_run = [
            e for e in result.get("evaluation_errors") or [] if _whole_run_failure(e)
        ]
        return _xray_whole_run_class(whole_run[0]) if whole_run else None
    if len(_xray_failed_cells(result)) < int(result["total_cells"]):
        return None
    return "no_cell_ran"


def xray_batch_repository_rows(
    result: Dict[str, Any], repositories: Sequence[str]
) -> List[Tuple[str, int, Optional[str]]]:
    """Rows of an X-Ray batch result (Bug #2111): one per resolved repository,
    `(alias, match count, error class or None)`.

    A repository with matches succeeded. Without matches it failed when the
    batch's Phase 1 failed or timed out, or when every one of its cells did
    not run; otherwise it ran and found nothing (a success). Raises
    ValueError for a result that is not a batch result (no total_cells).
    """
    if "total_cells" not in result:
        raise ValueError("an X-Ray batch result carries total_cells")
    cells_per_repository = int(result["total_cells"]) // max(1, len(repositories))
    counts = results_repository_counts(result.get("matches") or [], "repository_alias")
    failed_per_repository = Counter(alias for alias, _ in _xray_failed_cells(result))
    rows: List[Tuple[str, int, Optional[str]]] = []
    for alias in repositories:
        count = counts.get(alias, 0)
        error_class: Optional[str] = None
        if count == 0:
            if result.get("phase1_failed"):
                error_class = "phase1_failed"
            elif result.get("timeout"):
                error_class = "timeout"
            elif failed_per_repository[alias] >= cells_per_repository:
                error_class = "no_cell_ran"
        rows.append((alias, count, error_class))
    return rows


def scip_payload_outcome(payload: Dict[str, Any]) -> Optional[str]:
    """Outcome of a SCIP response payload (MCP or REST): a success carries
    `success` true and its result list. A handled error, a timeout and a
    rejected parameter answer success false."""
    if payload.get("success") is True and any(
        isinstance(payload.get(key), list) for key in _SCIP_RESULT_LISTS
    ):
        return None
    return _payload_error_class(payload)


def scip_payload_result_count(payload: Dict[str, Any]) -> int:
    """Number of results a SCIP response payload returned."""
    for key in _SCIP_RESULT_LISTS:
        if isinstance(payload.get(key), list):
            return len(payload[key])
    return 0
