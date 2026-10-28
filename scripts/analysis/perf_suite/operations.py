import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .client import (
    PerfClient,
    PollFailedError,
    RequestFailedError,
    ShapeError,
    TransportError,
)
from .corpus import Corpus, FanoutSpec, Query, hit_found
from .measure import Item

OPERATIONS = (
    "semantic",
    "semantic_cold",
    "fts",
    "regex",
    "xray",
    "files_list",
    "file_read",
    "fanout",
)
DOORS = ("rest", "mcp")
# ^ external REST file routes serve activated repos only; a -global alias answers 404
REST_UNAVAILABLE = frozenset({"files_list", "file_read"})
POLL_INTERVAL_S = 0.1
# ^ coupled with src/code_indexer/server/routes/xray_routes.py:_DEFAULT_TIMEOUT_SECONDS
SERVER_XRAY_JOB_TIMEOUT_S = 120
XRAY_POLL_MARGIN_S = 180
DEFAULT_XRAY_POLL_DEADLINE_S = SERVER_XRAY_JOB_TIMEOUT_S + XRAY_POLL_MARGIN_S
POLL_HTTP_TIMEOUT_S = 30.0
MAX_DETAIL_PATHS = 5
# ^ coupled with src/code_indexer/server/repositories/background_jobs.py:JobStatus; only completed is a pass
TERMINAL_STATUSES = (
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "completed_partial",
)


def door_available(operation: str, door: str) -> bool:
    return not (door == "rest" and operation in REST_UNAVAILABLE)


def _list(payload: Any, key: str, where: str) -> List[Any]:
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, list):
        raise ShapeError(f"{where}: response has no '{key}' list")
    return value


def _paths(entries: List[Any], key: str, where: str) -> List[str]:
    paths = []
    for entry in entries:
        value = entry.get(key) if isinstance(entry, dict) else None
        if not isinstance(value, str):
            raise ShapeError(f"{where}: result entry has no '{key}' string")
        paths.append(value)
    return paths


def rest_query_paths(payload: Any, mode: str) -> List[str]:
    if mode == "semantic":
        return _paths(
            _list(payload, "results", "POST /api/query"), "file_path", "POST /api/query"
        )
    return _paths(
        _list(payload, "fts_results", "POST /api/query"), "path", "POST /api/query"
    )


def mcp_search_paths(payload: Any) -> List[str]:
    inner = payload.get("results") if isinstance(payload, dict) else None
    return _paths(
        _list(inner, "results", "MCP search_code"), "file_path", "MCP search_code"
    )


def mcp_regex_paths(payload: Any) -> List[str]:
    return _paths(
        _list(payload, "matches", "MCP regex_search"), "file_path", "MCP regex_search"
    )


def xray_job_paths(job: Any) -> Optional[List[str]]:
    status = job.get("status") if isinstance(job, dict) else None
    if not isinstance(status, str):
        raise ShapeError("xray job has no status")
    if status not in TERMINAL_STATUSES:
        return None
    if status != "completed":
        raise RequestFailedError(f"xray job {status}: {job.get('error')}")
    return _paths(
        _list(job.get("result"), "matches", "xray job"), "file_path", "xray job"
    )


def list_files_paths(payload: Any) -> List[str]:
    return _paths(_list(payload, "files", "MCP list_files"), "path", "MCP list_files")


def file_content_text(payload: Any) -> str:
    parts = _list(payload, "file_content", "MCP get_file_content")
    return "".join(_paths(parts, "text", "MCP get_file_content"))


def _raise_repo_errors(errors: Any, where: str) -> None:
    if errors:
        raise RequestFailedError(f"{where} repository errors: {errors}")


def rest_multi_paths(payload: Any) -> Dict[str, List[str]]:
    if not isinstance(payload, dict):
        raise ShapeError("POST /api/query/multi: response is not an object")
    _raise_repo_errors(payload.get("errors"), "POST /api/query/multi")
    grouped = payload.get("results")
    if not isinstance(grouped, dict):
        raise ShapeError("POST /api/query/multi: response has no 'results' object")
    return {
        alias: _paths(rows if isinstance(rows, list) else [None], "file_path", alias)
        for alias, rows in grouped.items()
    }


def mcp_multi_paths(payload: Any) -> Dict[str, List[str]]:
    if not isinstance(payload, dict):
        raise ShapeError("MCP search_code multi: response is not an object")
    inner = payload.get("results")
    if not isinstance(inner, dict) or not isinstance(
        inner.get("results_by_repo"), dict
    ):
        raise ShapeError("MCP search_code multi: response has no results_by_repo")
    _raise_repo_errors(inner.get("errors"), "MCP search_code multi")
    return {
        alias: _paths(_list(group, "results", alias), "file_path", alias)
        for alias, group in inner["results_by_repo"].items()
    }


def regex_multi_paths(payload: Any) -> Dict[str, List[str]]:
    if not isinstance(payload, dict):
        raise ShapeError("multi-repo regex: response is not an object")
    _raise_repo_errors(payload.get("errors"), "multi-repo regex")
    grouped: Dict[str, List[str]] = {}
    for match in _list(payload, "matches", "multi-repo regex"):
        alias = match.get("source_repo") if isinstance(match, dict) else None
        if not isinstance(alias, str):
            raise ShapeError("multi-repo regex: match has no 'source_repo' string")
        grouped.setdefault(alias, []).extend(_paths([match], "file_path", alias))
    return grouped


def miss_detail(label: str, expect: str, hits: List[str]) -> Optional[str]:
    if hit_found(hits, expect):
        return None
    return f"{label}: expected {expect}; top {hits[:MAX_DETAIL_PATHS]}"


def fanout_miss(
    label: str,
    requested: List[str],
    grouped: Mapping[str, List[str]],
    expect_by_alias: Mapping[str, str],
) -> Optional[str]:
    absent = [alias for alias in requested if alias not in grouped]
    if absent:
        return f"{label}: no result group for {absent}"
    for alias, expect in expect_by_alias.items():
        detail = miss_detail(f"{label} [{alias}]", expect, grouped[alias])
        if detail is not None:
            return detail
    return None


def xray_request(
    door: str, alias: str, query: str, include: Tuple[str, ...], evaluator: str
) -> Dict[str, Any]:
    # ^ why no timeout_seconds, not a client override; the server's job timeout policy applies
    common = {
        "repository_alias": alias,
        "evaluator_code": evaluator,
        "search_target": "content",
        "include_patterns": list(include),
    }
    if door == "rest":
        return {**common, "driver_regex": query}
    return {**common, "pattern": query, "await_seconds": 0}


def poll_xray(
    perf: PerfClient,
    door: str,
    job_id: str,
    deadline_s: float = DEFAULT_XRAY_POLL_DEADLINE_S,
) -> List[str]:
    # ^ invariant loop ends at a terminal job, a raised poll error or deadline_s (safety net, not a work cap)
    if not 0 < deadline_s < float("inf"):
        raise ValueError("xray poll deadline must be a finite number > 0")
    poll_timeout = perf.http_timeout or POLL_HTTP_TIMEOUT_S
    deadline = time.monotonic() + deadline_s
    while True:
        if time.monotonic() >= deadline:
            raise RequestFailedError(
                f"poll deadline: xray job {job_id} not terminal after {deadline_s:g}s"
            )
        try:
            if door == "rest":
                job = perf.rest(
                    "GET",
                    "/api/jobs/{job_id}",
                    path_params={"job_id": job_id},
                    timeout=poll_timeout,
                )
            else:
                payload = perf.mcp(
                    "get_job_details", {"job_id": job_id}, timeout=poll_timeout
                )
                job = payload.get("job")
        except (RequestFailedError, TransportError) as exc:
            raise PollFailedError(f"polling xray job {job_id} failed: {exc}") from None
        paths = xray_job_paths(job)
        if paths is not None:
            return paths
        # ^ why fixed poll interval, not a busy loop; the job API has no server-side wait
        time.sleep(POLL_INTERVAL_S)


def _job_id(payload: Any, where: str) -> str:
    job_id = payload.get("job_id") if isinstance(payload, dict) else None
    if not isinstance(job_id, str):
        raise ShapeError(f"{where}: response has no job_id")
    return job_id


def _search(
    perf: PerfClient,
    corpus: Corpus,
    alias: str,
    q: Query,
    door: str,
    cold: bool,
    xray_poll_deadline_s: float = DEFAULT_XRAY_POLL_DEADLINE_S,
) -> List[str]:
    if q.mode == "xray":
        request = xray_request(door, alias, q.query, q.include, corpus.xray_evaluator)
        if door == "rest":
            job_id = _job_id(
                perf.rest("POST", "/api/xray/search", json=request),
                "POST /api/xray/search",
            )
        else:
            job_id = _job_id(perf.mcp("xray_search", request), "MCP xray_search")
        return poll_xray(perf, door, job_id, xray_poll_deadline_s)
    if q.mode == "regex":
        args = {
            "repository_alias": alias,
            "pattern": q.query,
            "max_results": corpus.top_n,
        }
        if door == "rest":
            return mcp_regex_paths(perf.rest("POST", "/api/regex/search", json=args))
        return mcp_regex_paths(perf.mcp("regex_search", args))
    search_mode = "semantic" if q.mode == "semantic" else "fts"
    if door == "rest":
        query_body: Dict[str, Any] = {
            "query_text": q.query,
            "repository_alias": alias,
            "search_mode": search_mode,
            "limit": corpus.top_n,
            "no_embedding_cache_shortcut": cold,
        }
        return rest_query_paths(
            perf.rest("POST", "/api/query", json=query_body), q.mode
        )
    args = {
        "repository_alias": alias,
        "query_text": q.query,
        "search_mode": search_mode,
        "limit": corpus.top_n,
        "no_embedding_cache_shortcut": cold,
    }
    return mcp_search_paths(perf.mcp("search_code", args))


def _query_item(
    perf: PerfClient,
    corpus: Corpus,
    alias: str,
    q: Query,
    door: str,
    cold: bool,
    xray_poll_deadline_s: float = DEFAULT_XRAY_POLL_DEADLINE_S,
) -> Item:
    label = f"{alias} {q.mode} {q.query or q.path!r}"

    def run() -> Optional[str]:
        if q.mode == "files_list":
            args = {"repository_alias": alias, "path": q.path}
            listed = list_files_paths(perf.mcp("list_files", args))
            return miss_detail(label, q.expect, listed)
        if q.mode == "file_read":
            args = {"repository_alias": alias, "file_path": q.path}
            text = file_content_text(perf.mcp("get_file_content", args))
            return None if q.expect in text else f"{label}: text {q.expect!r} absent"
        hits = _search(perf, corpus, alias, q, door, cold, xray_poll_deadline_s)
        return miss_detail(label, q.expect, hits)

    return run


def _fanout_item(
    perf: PerfClient,
    corpus: Corpus,
    aliases: Mapping[str, str],
    spec: FanoutSpec,
    door: str,
) -> Item:
    repos = [aliases[r.url] for r in corpus.repos]
    label = f"fanout {spec.mode} {spec.query!r}"
    expect_by_alias = {aliases[url]: path for url, path in spec.expect.items()}

    def run() -> Optional[str]:
        if spec.mode == "regex":
            args = {
                "repository_alias": repos,
                "pattern": spec.query,
                "max_results": corpus.top_n,
            }
            if door == "rest":
                payload = perf.rest("POST", "/api/regex/search", json=args)
            else:
                payload = perf.mcp("regex_search", args)
            regex_grouped = regex_multi_paths(payload)
            return fanout_miss(label, repos, regex_grouped, expect_by_alias)
        if door == "rest":
            body = {
                "repositories": repos,
                "query": spec.query,
                "search_type": spec.mode,
                "limit": corpus.top_n,
            }
            grouped = rest_multi_paths(perf.rest("POST", "/api/query/multi", json=body))
        else:
            args = {
                "repository_alias": repos,
                "query_text": spec.query,
                "search_mode": spec.mode,
                "limit": corpus.top_n,
            }
            grouped = mcp_multi_paths(perf.mcp("search_code", args))
        return fanout_miss(label, repos, grouped, expect_by_alias)

    return run


def build_items(
    perf: PerfClient,
    corpus: Corpus,
    aliases: Mapping[str, str],
    operation: str,
    door: str,
    xray_poll_deadline_s: float = DEFAULT_XRAY_POLL_DEADLINE_S,
) -> List[Item]:
    if operation not in OPERATIONS or door not in DOORS:
        raise ValueError(f"unknown operation/door {operation}/{door}")
    if operation == "fanout":
        return [
            _fanout_item(perf, corpus, aliases, spec, door) for spec in corpus.fanout
        ]
    cold = operation == "semantic_cold"
    mode = "semantic" if cold else operation
    return [
        _query_item(
            perf, corpus, aliases[repo.url], q, door, cold, xray_poll_deadline_s
        )
        for repo in corpus.repos
        for q in repo.queries
        if q.mode == mode
    ]
