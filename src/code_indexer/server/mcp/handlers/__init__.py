"""MCP handler package — backward-compatible namespace.

Phase 2+: This package imports all symbols from _legacy.py (and eventually
from individual domain modules) into its own namespace so that:

  - patch("code_indexer.server.mcp.handlers.X") patches the binding in this
    __init__.py, which is what external code sees — preserving all mock patches.
  - Domain submodules (search.py, repos.py, ...) can be imported as
    code_indexer.server.mcp.handlers.search etc.

As each domain module is extracted from _legacy.py, the corresponding
'from ._legacy import X' line below is replaced with 'from .domain import X'.
"""

# Re-export everything from _legacy so the package namespace is complete.
# Explicit star-import makes this a real package (not a sys.modules alias)
# which allows submodule imports (handlers.search, handlers.repos, etc.).
# Note: star-import does NOT re-export private names (starting with _), so
# we must list them explicitly below.
from code_indexer.server.mcp.handlers._legacy import *  # noqa: F401, F403

# Explicitly re-export private names used by tests and external consumers.
# Star-import skips names beginning with '_', so they must be listed here.
# Sources: _utils.py (shared utilities) and _legacy.py (domain-specific helpers).
from code_indexer.server.mcp.handlers._utils import (  # noqa: F401
    # Symbols that live in _utils.py
    app_module,  # canonical home — tests patch handlers._utils.app_module
    _has_wildcard,
    _truncate_field,
    _truncate_regex_field,
    WILDCARD_CHARS,
)

import sys as _sys
import types as _types
from typing import Any as _Any


class _ForwardingModule(_types.ModuleType):
    """Module class that forwards setattr to _legacy for mock-patch compatibility.

    When handlers.py was a flat module, ``patch("handlers.X")`` replaced
    ``X`` in the module's global dict, and callers in the same module saw
    the patched value.  Now that handlers is a *package*, callers live in
    ``_legacy.py``, whose global dict is separate.  Without forwarding,
    patching ``handlers.X`` replaces the re-exported binding in
    ``__init__.py`` but leaves ``_legacy.X`` untouched, so callers never
    see the mock.

    This ``__setattr__`` intercept mirrors every attribute write on the
    package namespace into ``_legacy``'s namespace (when the name exists
    there), making all existing ``patch("handlers.X")`` calls work
    transparently without touching any test file.
    """

    def __setattr__(self, name: str, value: _Any) -> None:
        # Always set on ourselves first.
        super().__setattr__(name, value)
        if not name.startswith("__"):
            # Propagate into _legacy so callers there see the patched binding.
            # Callers in _legacy.py look up names in _legacy's own global dict,
            # not in this package __init__.  Without forwarding, patch("handlers.X")
            # replaces only the __init__ copy, leaving _legacy.X untouched.
            legacy = _sys.modules.get("code_indexer.server.mcp.handlers._legacy")
            if legacy is not None and name in legacy.__dict__:
                legacy.__dict__[name] = value
            # Propagate into extracted domain submodules so that callers in
            # those modules also see the patched binding.  When a handler moves
            # from _legacy.py to, say, scip.py, it imports helpers directly
            # from _utils (e.g. `from ._utils import _get_scip_query_service`).
            # Tests that patch "handlers._get_scip_query_service" must therefore
            # also update the binding in each domain module where it is used.
            for _submod_name in (
                "code_indexer.server.mcp.handlers.scip",
                "code_indexer.server.mcp.handlers.guides",
                "code_indexer.server.mcp.handlers.ssh_keys",
                "code_indexer.server.mcp.handlers.delegation",
                "code_indexer.server.mcp.handlers.pull_requests",
            ):
                _submod = _sys.modules.get(_submod_name)
                if _submod is not None and name in _submod.__dict__:
                    _submod.__dict__[name] = value
            # app_module lives in _utils and is accessed as _utils.app_module
            # in _legacy.py (not as a bare name).  Forward writes here so that
            # tests which set handlers.app_module = mock also affect _utils.
            if name == "app_module":
                utils = _sys.modules.get("code_indexer.server.mcp.handlers._utils")
                if utils is not None:
                    utils.__dict__["app_module"] = value


_sys.modules[__name__].__class__ = _ForwardingModule


from code_indexer.server.mcp.handlers.guides import (  # noqa: F401, E402
    # Public handlers extracted from _legacy (Story #496 step 3)
    quick_reference,
    first_time_user_guide,
    get_tool_categories,
    handle_start_trace,
    handle_end_trace,
    handle_wiki_article_analytics,
    # Private helpers used by tests and external consumers
    _get_wiki_cache_for_handler,
    _wiki_analytics_filter_by_search,
    _wiki_analytics_build_articles,
)

from code_indexer.server.mcp.handlers.scip import (  # noqa: F401, E402
    # Public handlers extracted from _legacy (Story #496 scip step)
    scip_definition,
    scip_references,
    scip_dependencies,
    scip_dependents,
    scip_impact,
    scip_callchain,
    scip_context,
    get_scip_audit_log,
    handle_scip_pr_history,
    handle_scip_cleanup_history,
    handle_scip_cleanup_workspaces,
    handle_scip_cleanup_status,
)

from code_indexer.server.mcp.handlers._legacy import (  # noqa: F401, E402
    # Package-level attributes expected by protocol.py and tests
    HANDLER_REGISTRY,
    # Utilities (originally in _legacy, now imported there from _utils)
    _apply_fts_payload_truncation,
    _apply_payload_truncation,
    _apply_regex_payload_truncation,
    _apply_scip_payload_truncation,
    _apply_temporal_payload_truncation,
    _coerce_float,
    _coerce_int,
    _enrich_with_wiki_url,
    _error_with_suggestions,
    _expand_wildcard_patterns,
    _format_omni_response,
    _get_access_filtering_service,
    _get_app_refresh_scheduler,
    _get_available_repos,
    _get_global_repo,
    _get_golden_repos_dir,
    _get_hnsw_health_service,
    _get_query_tracker,
    _get_scip_audit_repository,
    _get_scip_query_service,
    _get_temporal_status,
    _get_wiki_enabled_repos,
    _is_temporal_query,
    _list_global_repos,
    _mcp_response,
    _parse_json_string_array,
    _validate_symbol_format,
    # Domain helpers in _legacy.py (not yet extracted to domain modules)
    _append_provider_to_config,
    _derive_forge_host,
    _get_group_manager,
    _get_pat_credential_for_remote,
    _get_personal_credential_for_host,
    _omni_regex_search,
    _omni_search_code,
    _provider_index_job,
    _provider_temporal_index_job,
    _remove_provider_from_config,
    _resolve_cicd_project_access,
    _resolve_cicd_read_token,
    _resolve_cicd_write_token,
    _resolve_git_repo_path,
    _resolve_golden_repo_base_clone,
    _resolve_golden_repo_path,
    _resolve_repo_path,
    _write_mode_acquire_lock,
    _write_mode_create_marker,
    _write_mode_run_refresh,
    _write_mode_strip_global,
    _is_write_mode_active,
    _post_provider_index_snapshot,
    # CI/CD handlers (Story #745)
    handle_gh_actions_list_runs,
    handle_gh_actions_get_run,
    handle_gh_actions_search_logs,
    handle_gh_actions_get_job_logs,
    handle_gh_actions_retry_run,
    handle_gh_actions_cancel_run,
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
)

from code_indexer.server.mcp.handlers.delegation import (  # noqa: F401, E402
    # Public handlers extracted from _legacy (Story #496 delegation step)
    handle_list_delegation_functions,
    handle_execute_delegation_function,
    handle_poll_delegation_job,
    handle_execute_open_delegation,
    handle_cs_register_repository,
    handle_cs_list_repositories,
    handle_cs_check_health,
    # Private helpers used by tests and external consumers
    _get_cidx_callback_base_url,
    _get_delegation_config,
    _get_delegation_function_repo_path,
    _get_repo_ready_timeout,
    _get_user_groups,
    _load_packages_context,
    _lookup_golden_repo_for_cs,
    _resolve_guardrails,
    _validate_collaborative_params,
    _validate_competitive_params,
    _validate_function_parameters,
    _validate_open_delegation_params,
)

from code_indexer.server.mcp.handlers.pull_requests import (  # noqa: F401, E402
    # Public handlers extracted from _legacy (Story #496 pull_requests step)
    create_pull_request,
    list_pull_requests,
    get_pull_request,
    list_pull_request_comments,
    comment_on_pull_request,
    update_pull_request,
    merge_pull_request,
    close_pull_request,
)
