"""Regex (FTS/ripgrep) search handlers.

Domain module for search handlers. Part of the handlers package
modularization (Story #496).

Issue #1935 Part: split out of the former flat search.py (2,498 lines)
into this package, one module per domain seam, each < 1,000 lines.
Pure move -- zero behaviour change.

NOTE: Functions in this module were extracted verbatim from _legacy.py
(via search.py). Pre-existing method lengths and duplication are
preserved intentionally to avoid behavioral changes during extraction.
Refactoring is tracked separately.
"""

import asyncio
import functools
import logging
import time
from pathlib import Path
from typing import Any, Dict

import anyio.to_thread

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.mcp import reranking as _mcp_reranking
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.query_admission_gate import (
    check_query_admission,
    memory_pressure_mcp_payload,
)
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)
from code_indexer.server.telemetry.manager import peek_telemetry_manager
from code_indexer.server.telemetry.metrics_instrumentation import (
    get_application_metrics,
)

from .. import _utils
from .._utils import (
    CapBreach,
    cap_breach_response,
    _apply_regex_payload_truncation,
    _coerce_int,
    _enforce_repo_count_cap,
    _enrich_with_wiki_url,
    _expand_wildcard_patterns,
    _format_omni_response,
    _get_access_filtering_service,
    _get_golden_repos_dir,
    _get_wiki_enabled_repos,
    _has_wildcard,
    _mcp_response,
    _parse_json_string_array,
)
from ._shared import (
    _DEFAULT_REGEX_CONTEXT_LINES,
    _DEFAULT_REGEX_MAX_RESULTS,
    _MAX_REGEX_CONTEXT_LINES,
    _MAX_REGEX_MAX_RESULTS,
    _filter_errors_for_user,
    _get_legacy,
)

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


async def _omni_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-regex search across multiple repositories.

    Extracted from _legacy.py lines 1957-2041.
    """
    import json as json_module

    repo_aliases = _expand_wildcard_patterns(args.get("repository_alias", []), user)
    if isinstance(repo_aliases, CapBreach):
        return cap_breach_response(repo_aliases)
    # Bug #894: enforce total fan-out cap after wildcard expansion + literal union
    _repo_count_breach = _enforce_repo_count_cap(repo_aliases)
    if _repo_count_breach is not None:
        return cap_breach_response(_repo_count_breach)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "matches": [],
                "total_matches": 0,
                "truncated": False,
                "read_capped": False,
                "search_engine": "ripgrep",
                "search_time_ms": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    start_time = time.time()
    all_matches: list = []
    errors: dict = {}
    repos_searched = 0
    truncated = False
    read_capped = False

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias
            single_result = await handle_regex_search(single_args, user)

            content = single_result.get("content", [])
            if content and content[0].get("type") == "text":
                result_data = json_module.loads(content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    matches = result_data.get("matches", [])
                    for m in matches:
                        m["source_repo"] = repo_alias
                    all_matches.extend(matches)
                    if result_data.get("truncated"):
                        truncated = True
                    if result_data.get("read_capped"):
                        read_capped = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-039",
                    f"Omni-regex failed for {repo_alias}: {e}",
                )
            )

    elapsed_ms = int((time.time() - start_time) * 1000)
    errors = _filter_errors_for_user(errors, user)

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_matches,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["truncated"] = truncated
    formatted["read_capped"] = read_capped
    formatted["search_engine"] = "ripgrep"
    formatted["search_time_ms"] = elapsed_ms
    if response_format == "flat":
        formatted["matches"] = formatted.pop("results")
        # Issue #1601 Priority 7 (documented deliberate deferral, not an
        # oversight): this omni-level total_matches stays len(all_matches)
        # (the sum of returned per-repo PAGES) rather than a sum of
        # per-repo lower-bound SENTINELS (Priority 7's single-repo fix
        # above). _format_omni_response is a shared helper also used by
        # non-regex omni handlers (files.py, git_read.py, semantic
        # search) with no equivalent sentinel concept, and naively
        # summing per-repo sentinels here would produce a number that
        # mixes exact counts with lower bounds inconsistently unless
        # every constituent additionally reported whether ITS OWN total
        # was itself a sentinel -- a larger protocol change out of scope
        # for this remediation. The per-repo read_capped/truncated flags
        # above are already OR-aggregated and remain the authoritative
        # "results may be incomplete" signal for the omni response.
        formatted["total_matches"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def _validate_regex_args(args: Dict[str, Any]) -> tuple:
    """Validate and normalize regex search arguments.

    Returns:
        (repository_alias, None) on success.
        (None, mcp_error_response) on validation failure.
    """
    include_patterns = args.get("include_patterns")
    if include_patterns is not None and not isinstance(include_patterns, list):
        return None, _mcp_response(
            {"success": False, "error": "include_patterns must be a list of strings"}
        )

    exclude_patterns = args.get("exclude_patterns")
    if exclude_patterns is not None and not isinstance(exclude_patterns, list):
        return None, _mcp_response(
            {"success": False, "error": "exclude_patterns must be a list of strings"}
        )

    # Bug #1876 item 4: validate and compile every pattern ONCE here, at
    # the front door -- a non-string item (e.g. exclude_patterns=[None])
    # or a malformed pattern (unbalanced brace, gitignore negation/comment
    # syntax) must never silently pass through. Previously an invalid
    # exclude pattern failed OPEN (silently widened the search) with zero
    # signal to the caller.
    from code_indexer.services.path_pattern_matcher import (
        InvalidPatternError,
        PathPatternMatcher,
    )

    matcher = PathPatternMatcher()
    for field_name, patterns in (
        ("include_patterns", include_patterns),
        ("exclude_patterns", exclude_patterns),
    ):
        try:
            matcher.compile_patterns(patterns)
        except InvalidPatternError as e:
            return None, _mcp_response(
                {
                    "success": False,
                    "error": f"invalid {field_name}: {e}",
                }
            )

    repository_alias = _parse_json_string_array(args.get("repository_alias"))
    args["repository_alias"] = repository_alias

    if not args.get("pattern"):
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: pattern"}
        )

    if isinstance(repository_alias, list):
        if not repository_alias:
            return None, _mcp_response(
                {"success": False, "error": "repository_alias list must not be empty"}
            )
    elif not repository_alias:
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    return repository_alias, None


def _build_and_enrich_matches(search_result, repository_alias: str) -> list:
    """Build match dicts from a RegexSearchResult and enrich with wiki URLs.

    Extracted from the former body of _execute_regex_search (Story #1586
    AC1 split) so no single function there exceeds the method-length limit.
    """
    matches = [
        {
            "file_path": m.file_path,
            "line_number": m.line_number,
            "column": m.column,
            "line_content": m.line_content,
            "context_before": m.context_before,
            "context_after": m.context_after,
        }
        for m in search_result.matches
    ]

    wiki_enabled_repos = _get_wiki_enabled_repos()
    for match in matches:
        _enrich_with_wiki_url(
            match,
            match.get("file_path", ""),
            repository_alias,
            wiki_enabled_repos,
        )

    return matches


async def _rerank_truncate_and_filter_regex_matches(
    matches: list,
    args: Dict[str, Any],
    repository_alias: str,
    user: User,
) -> tuple:
    """Rerank, truncate, and (for cidx-meta repos) access-filter matches.

    Extracted from the former body of _execute_regex_search (Story #1586
    AC1 split).

    Returns:
        (matches, rerank_meta). rerank_meta carries an additional
        "cidx_meta_access_filtered" bool (Bug #337 regression fix) set True
        only when the cidx-meta access-filtering branch below actually ran
        with a real access_svc -- the caller (handle_regex_search) uses this
        to decide whether the raw engine's sr.total_matches (Issue #1601
        Priority 7's lower-bound sentinel) is still safe to report, or
        whether it must fall back to the post-filter len(matches) instead
        because the raw count would otherwise leak the existence of files
        the requesting user is not authorized to see.
    """
    regex_limit = _coerce_int(args.get("max_results"), len(matches))
    rerank_kwargs = dict(
        results=matches,
        rerank_query=args.get("rerank_query"),
        rerank_instruction=args.get("rerank_instruction"),
        content_extractor=lambda r: r.get("line_content", "") or "",
        requested_limit=regex_limit,
        config_service=get_config_service(),
    )
    matches, rerank_meta = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _mcp_reranking._apply_reranking_sync(**rerank_kwargs)
    )

    matches = _apply_regex_payload_truncation(matches)

    cidx_meta_access_filtered = False
    if repository_alias and "cidx-meta" in repository_alias:
        access_svc = _get_access_filtering_service()
        if access_svc:
            filenames = [Path(m["file_path"]).name for m in matches]
            allowed = set(access_svc.filter_cidx_meta_files(filenames, user.username))
            matches = [m for m in matches if Path(m["file_path"]).name in allowed]
            cidx_meta_access_filtered = True
    rerank_meta["cidx_meta_access_filtered"] = cidx_meta_access_filtered

    return matches, rerank_meta


async def _execute_regex_search_impl(
    args: Dict[str, Any],
    repo_path: Path,
    repository_alias: str,
    user: User,
) -> tuple:
    """Execute regex search, enrich, rerank, truncate, and filter results.

    Returns:
        (matches, rerank_meta, search_result) where search_result is the
        raw RegexSearchResult dataclass.
    """
    from code_indexer.global_repos.regex_search import RegexSearchService

    config = get_config_service().get_config()
    search_limits = config.search_limits_config
    subprocess_max_workers = config.background_jobs_config.subprocess_max_workers
    max_results = max(
        1,
        min(
            _MAX_REGEX_MAX_RESULTS,
            _coerce_int(args.get("max_results"), _DEFAULT_REGEX_MAX_RESULTS),
        ),
    )
    context_lines = max(
        0,
        min(
            _MAX_REGEX_CONTEXT_LINES,
            _coerce_int(args.get("context_lines"), _DEFAULT_REGEX_CONTEXT_LINES),
        ),
    )

    # Issue #1609: RegexSearchService.__init__ performs synchronous
    # filesystem calls (Path.resolve(), shutil.which()) that must never
    # run directly on the event-loop thread inside an `async def` (this
    # project's own Production Scale invariant). Offload the constructor
    # call itself via anyio.to_thread.run_sync, mirroring the granular
    # per-call offload pattern already established elsewhere in this
    # search path (see regex_search.py's search() method).
    service = await anyio.to_thread.run_sync(
        functools.partial(
            RegexSearchService,
            repo_path,
            subprocess_max_workers=subprocess_max_workers,
            # Issue #1601 Priority 9: thread the already-in-scope alias
            # through so the service's read-capped WARNING log surfaces the
            # real user-facing alias, not just the (possibly
            # versioned-snapshot) repo_path.
            alias=repository_alias,
        )
    )
    search_result = await service.search(
        pattern=args["pattern"],
        path=args.get("path"),
        include_patterns=args.get("include_patterns"),
        exclude_patterns=args.get("exclude_patterns"),
        case_sensitive=args.get("case_sensitive", True),
        context_lines=context_lines,
        max_results=max_results,
        timeout_seconds=search_limits.timeout_seconds,
        multiline=args.get("multiline", False),
        pcre2=args.get("pcre2", False),
    )

    matches = _build_and_enrich_matches(search_result, repository_alias)
    matches, rerank_meta = await _rerank_truncate_and_filter_regex_matches(
        matches, args, repository_alias, user
    )

    return matches, rerank_meta, search_result


def _record_fts_metric(
    repository_alias: str,
    duration_seconds: float,
    matches_count: int,
    status: str,
) -> None:
    """Record a cidx.fts.* OTEL metric for one _execute_regex_search call
    (Story #1586 AC1). No-op when telemetry is disabled (ApplicationMetrics
    early-returns internally); never raises into the regex search call path.
    """
    try:
        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        app_metrics.record_fts_request(
            repository=repository_alias,
            duration_seconds=duration_seconds,
            matches_count=matches_count,
            status=status,
        )
    except Exception as e:
        logger.debug(f"Failed to record FTS metrics: {e}")


async def _execute_regex_search(
    args: Dict[str, Any],
    repo_path: Path,
    repository_alias: str,
    user: User,
) -> tuple:
    """Execute regex search and record its cidx.fts.* OTEL metric (AC1).

    Thin wrapper around _execute_regex_search_impl(): times the call and
    records success/error via _record_fts_metric() in `finally`, so the
    metric is recorded exactly once regardless of outcome.
    """
    _fts_metric_start = time.monotonic()
    matches_count = 0
    fts_status = "error"
    try:
        (
            matches,
            rerank_meta,
            search_result,
        ) = await _execute_regex_search_impl(args, repo_path, repository_alias, user)
        matches_count = len(matches)
        fts_status = "success"
        return matches, rerank_meta, search_result
    finally:
        _record_fts_metric(
            repository_alias,
            time.monotonic() - _fts_metric_start,
            matches_count,
            fts_status,
        )


async def handle_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for regex_search tool - pattern matching with timeout protection.

    Extracted from _legacy.py lines 2044-2220.
    """
    _admission = check_query_admission()
    if not _admission.allowed:
        return _mcp_response(memory_pressure_mcp_payload(_admission))

    from code_indexer.server.services.search_error_formatter import SearchErrorFormatter

    repository_alias, err = _validate_regex_args(args)
    if err is not None:
        return err  # type: ignore[no-any-return]  # err is dict from _mcp_response

    # Bug #1119: a wildcard string like "*" or "fastapi-?" must be routed to the
    # omni/expansion path, not to the single-repo path. Wrap the wildcard string as
    # a one-element list so the routing below sends it to _omni_regex_search where
    # _expand_wildcard_patterns can expand it properly.
    if (
        isinstance(repository_alias, str)
        and repository_alias
        and _has_wildcard(repository_alias)
    ):
        repository_alias = [repository_alias]
        args["repository_alias"] = repository_alias

    if isinstance(repository_alias, list):
        return await _omni_regex_search(args, user)

    # Bug #721: regex_search is in _SELF_TRACKING_TOOLS, so handler must track itself.
    # Placed after the omni branch: _omni_regex_search recurses into handle_regex_search
    # once per repo with a single alias, so each recursive call increments here —
    # giving exactly N increments for N repos, with no double-count at the omni entry.
    api_metrics_service.increment_regex_search(username=user.username)

    # Story #1039: bare-to-global alias fallback (read-only handler).
    # Bug #1770: regex_search's single-repo resolution previously ALWAYS fell
    # through to the golden-repo-only legacy resolver (_resolve_repo_path),
    # which has no knowledge of user-activated repositories. search_code
    # never hits this because it delegates activated-repo resolution to
    # query_user_repositories (via ActivatedRepoManager) internally. Track
    # whether the user genuinely has this alias activated here -- reusing the
    # SAME user_has_activated_repo() check that already gates the fallback --
    # so the resolution step below can route to ActivatedRepoManager directly
    # for activated repos, exactly like _resolve_temporal_repo_path already
    # does, instead of reinventing a third resolution mechanism. This check
    # depends ONLY on activated_repo_manager being available -- it must not
    # be skipped merely because golden_repo_manager (needed only for the
    # global-fallback promotion below) happens to be unavailable.
    _user_has_activated_repo = False
    _arm = getattr(_utils.app_module, "activated_repo_manager", None)
    if isinstance(repository_alias, str) and not repository_alias.endswith("-global"):
        if _arm is not None:
            _user_has_activated_repo = _arm.user_has_activated_repo(
                user.username, repository_alias
            )
        if not _user_has_activated_repo:
            _grm = getattr(_utils.app_module, "golden_repo_manager", None)
            if _arm is not None and _grm is not None:
                from .._global_fallback import try_global_fallback

                _promoted = try_global_fallback(repository_alias, _grm)
                if _promoted is not None:
                    logger.info(
                        "bare-alias fallback: %r -> %r for user %r",
                        repository_alias,
                        _promoted,
                        user.username,
                    )
                    args["repository_alias"] = _promoted
                    repository_alias = _promoted

    try:
        if (
            _user_has_activated_repo
            and isinstance(repository_alias, str)
            and not repository_alias.endswith("-global")
            and _arm is not None
        ):
            resolved = _arm.get_activated_repo_path(user.username, repository_alias)
            if resolved and not Path(resolved).exists():
                resolved = None
        else:
            golden_repos_dir = _get_golden_repos_dir()
            resolved = _get_legacy()._resolve_repo_path(
                repository_alias, golden_repos_dir
            )
        if not resolved:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Repository '{repository_alias}' not found",
                }
            )

        matches, rerank_meta, sr = await _execute_regex_search(
            args, Path(resolved), repository_alias, user
        )
        # Bug #337 regression fix: when cidx-meta access filtering actually
        # ran (rerank_meta["cidx_meta_access_filtered"], set in
        # _rerank_truncate_and_filter_regex_matches), sr.total_matches is
        # the PRE-filter raw engine count -- reporting it would both be
        # factually wrong for the user and leak the existence-count of
        # files they are not authorized to see. Fall back to the
        # post-filter len(matches) in that case. Otherwise (non-cidx-meta
        # repos, or no filtering applied), Issue #1601 Priority 7's
        # original behavior is preserved: propagate the service's own
        # total_matches (a deliberate lower-bound SENTINEL, e.g.
        # max_results + 1, once truncated/read_capped stopped the scan
        # early) -- never recompute via len(matches), which would silently
        # discard that signal.
        total_matches = (
            len(matches)
            if rerank_meta.get("cidx_meta_access_filtered")
            else sr.total_matches
        )
        return _mcp_response(
            {
                "success": True,
                "matches": matches,
                "total_matches": total_matches,
                "truncated": sr.truncated,
                "read_capped": sr.read_capped,
                "search_engine": sr.search_engine,
                "search_time_ms": sr.search_time_ms,
                "query_metadata": {
                    "reranker_used": rerank_meta["reranker_used"],
                    "reranker_provider": rerank_meta["reranker_provider"],
                    "rerank_time_ms": rerank_meta["rerank_time_ms"],
                },
            }
        )
    except TimeoutError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-040",
                f"Search timeout in regex_search: {e}",
            )
        )
        error_formatter = SearchErrorFormatter()
        search_limits = get_config_service().get_config().search_limits_config
        error_data = error_formatter.format_timeout_error(
            timeout_seconds=search_limits.timeout_seconds,
            partial_results=None,
        )
        return _mcp_response({"success": False, **error_data})
    except Exception as e:
        logger.exception(
            f"Error in regex_search: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_regex_search_sync(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Sync-dispatched entry point for the regex_search MCP tool (AC2).

    Story #1491 AC2 (report Finding B2): ``handle_regex_search`` is async, so
    ``protocol.py``'s dispatcher took the ``await handler(...)`` branch and ran
    the handler's SYNCHRONOUS sections directly on the event loop -- the SQLite
    trigram prefilter, up to ``_MAX_PREFILTER_CANDIDATES`` (8000)
    ``Path.resolve()`` calls, the whole multi-MB ripgrep JSON output read, and
    the per-line ``json.loads`` plus per-match resolve. An omni (``*``) search
    multiplies all of it by the repo count.

    Registering this plain ``def`` instead sends the tool down the dispatcher's
    sync branch (``run_in_executor``), so the entire handler -- awaitable and
    synchronous parts alike -- runs on a worker thread and the loop stays free.

    Deliberate, documented consequence for the Issue #1398 sync/async dispatch
    distinction: ``regex_search`` is now SYNC-dispatched. Its MCP-layer timeout
    semantics are unchanged nonetheless -- ``regex_search`` is a member of
    ``_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS``, which the dispatcher honours on
    BOTH branches, so the tool remains governed solely by its own configured
    ``search_limits_config.timeout_seconds`` plus its ripgrep subprocess
    timeout (Issue #1398 Group A), never by a handler-level deadline.

    The coroutine runs on a private loop owned by this worker thread. Nothing
    it awaits is shared across loops: ``RegexSearchService`` is constructed per
    request and its ``SubprocessExecutor`` owns a per-call thread pool.
    """
    return asyncio.run(handle_regex_search(args, user))
