"""The ONE module of ``cidx_depmap`` that imports the code-indexer core.

The dependency map is meant to import ``cidx_sdk`` and third-party packages
only. The server pieces it still needs -- the LLM CLI dispatcher, the
catalog's dependency-map file format and parsers, the cidx-meta writer and
refresh, the job tracker, the lifecycle batch runner, the activity journal,
the dashboard storage backends, the Web UI session helpers -- are not in the
SDK yet, so this adapter reaches them in ``code_indexer`` and every other
module of the package imports them from here. When the SDK provides them,
this file switches to the SDK and nothing else changes. The import contract
``depmap-feature-no-core`` (``.importlinter``) counts these imports in its
ratchet baseline; no other module of the package may add one.

Every name below is resolved in its core module on first use (``__getattr__``):
importing a light module of the feature loads nothing of the server, and a
test that patches a core binding (``code_indexer.server.services.X.name``)
is seen by a function-level import. The ``TYPE_CHECKING`` block names each
one for the type checker.

=============================  ============================================  ===========================
Group                          Used for                                      Replaced by
=============================  ============================================  ===========================
LLM dispatch                   ``build_dep_map_dispatcher``, invokers,       a CLI-invoker capability
                               ``agent_cli_isolation``, prompts
catalog format                 ``dep_map_parser_*``, ``dep_map_file_utils``, SDK utilities (the core's
                               ``dep_map_source`` (the core's catalog        catalog projection parses
                               projection parses the same files)             the same format)
catalog writes / reads         ``request_cidx_meta_refresh``,                a catalog-writes capability
                               ``synced_read_root``, ``catalog_read_path``,
                               ``content_seam_source``, ``manifest_entries``
                               ``pod_directory``
jobs                           ``job_tracker``, ``SharedJobSentinel``,       the ``background_jobs``
                               ``DuplicateJobError``, lifecycle batch        capability, extended
                               runner, jittered dispatch
storage                        tracking / dashboard-cache backends,          the ``storage`` capability
                               ``DatabaseConnectionManager``
Web UI                         session manager, admin session, login         the SDK web contract
                               redirect, golden repository manager
``server_platform()``          the app state and the MCP envelope            SDK capabilities
=============================  ============================================  ===========================

The catalog access projection is NOT reached here: the MCP tools and the web
pages take each caller's dependency-map view from the ``access_projection``
capability (``server/mcp/handlers.py``, ``server/web/caller_view.py``).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Dict, Tuple

from code_indexer.kernel import capabilities as capabilities

if TYPE_CHECKING:
    #: The core web shell's ``Jinja2Templates`` (``web.pages._shared.templates``).
    shell_templates: Any
    from code_indexer.global_repos.lifecycle_batch_runner import (
        LifecycleBatchRunner as LifecycleBatchRunner,
        LifecycleFleetScanner as LifecycleFleetScanner,
    )
    from code_indexer.global_repos.meta_description_hook import (
        request_cidx_meta_refresh as request_cidx_meta_refresh,
    )
    from code_indexer.global_repos.prompts import (
        get_prompt as get_prompt,
    )
    from code_indexer.global_repos.write_lock_manager import (
        describe_scheduler_lock_holder as describe_scheduler_lock_holder,
    )
    from code_indexer.global_repos.yaml_emitter_utils import (
        yaml_quote_if_unsafe as yaml_quote_if_unsafe,
    )
    from code_indexer.server.app import (
        app as app,
        background_job_manager as background_job_manager,
        job_tracker as job_tracker,
    )
    from code_indexer.server.auth import dependencies as dependencies
    from code_indexer.server.mcp.handlers._utils import (
        _mcp_response as _mcp_response,
    )
    from code_indexer.server.services.activity_journal_service import (
        ActivityJournalService as ActivityJournalService,
    )
    from code_indexer.server.services.agent_cli_isolation import (
        prepare_stable_neutral_cwd as prepare_stable_neutral_cwd,
        remove_mcp_config_file as remove_mcp_config_file,
        try_build_mcp_config_file as try_build_mcp_config_file,
    )
    from code_indexer.server.services.backfill_journal_service import (
        BackfillJournalService as BackfillJournalService,
    )
    from code_indexer.server.services.cidx_meta_read_mirror import (
        catalog_read_path as catalog_read_path,
        DEPENDENCY_MAP_DIR as DEPENDENCY_MAP_DIR,
        synced_read_root as synced_read_root,
    )
    from code_indexer.server.services.claude_invoker import (
        ClaudeInvoker as ClaudeInvoker,
    )
    from code_indexer.server.services.cli_dispatcher import (
        CliDispatcher as CliDispatcher,
    )
    from code_indexer.server.services.codex_invoker import (
        CodexInvoker as CodexInvoker,
    )
    from code_indexer.server.services.codex_mcp_auth_header_provider import (
        build_codex_mcp_auth_header_provider as build_codex_mcp_auth_header_provider,
    )
    from code_indexer.server.services.config_service import (
        get_config_service as get_config_service,
    )
    from code_indexer.server.services.constants import (
        CIDX_META_REPO as CIDX_META_REPO,
    )
    from code_indexer.server.services.dep_map_dispatcher_factory import (
        build_dep_map_dispatcher as build_dep_map_dispatcher,
    )
    from code_indexer.server.services.dep_map_file_utils import (
        get_domain_md_files as get_domain_md_files,
        has_yaml_frontmatter as has_yaml_frontmatter,
        load_domains_json as load_domains_json,
        parse_simple_yaml as parse_simple_yaml,
        parse_yaml_frontmatter as parse_yaml_frontmatter,
    )
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyAggregate as AnomalyAggregate,
        AnomalyEntry as AnomalyEntry,
        AnomalyType as AnomalyType,
        aggregate_anomalies as aggregate_anomalies,
        deduplicate_anomalies as deduplicate_anomalies,
        is_prose_fragment as is_prose_fragment,
        normalize_identifier as normalize_identifier,
        strip_backticks as strip_backticks,
    )
    from code_indexer.server.services.dep_map_parser_tables import (
        build_cross_domain_connections as build_cross_domain_connections,
        build_name_description as build_name_description,
        build_participating_repos as build_participating_repos,
        iter_table_rows as iter_table_rows,
        parse_frontmatter_strict as parse_frontmatter_strict,
        parse_incoming_table as parse_incoming_table,
        parse_last_analyzed as parse_last_analyzed,
        parse_roles_table as parse_roles_table,
    )
    from code_indexer.server.services.dep_map_source import (
        DepMapSource as DepMapSource,
        FilesystemDepMapSource as FilesystemDepMapSource,
    )
    from code_indexer.server.services.file_service import (
        content_seam_source as content_seam_source,
        manifest_entries as manifest_entries,
        pod_directory as pod_directory,
    )
    from code_indexer.server.services.jittered_dispatcher import (
        DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS as DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS,
        DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS as DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS,
        sleep_with_jitter as sleep_with_jitter,
    )
    from code_indexer.server.services.job_tracker import (
        DuplicateJobError as DuplicateJobError,
    )
    from code_indexer.server.services.metadata_reader import (
        read_current_commit as read_current_commit,
    )
    from code_indexer.server.services.query_admission_gate import (
        check_query_admission as check_query_admission,
        memory_pressure_mcp_payload as memory_pressure_mcp_payload,
    )
    from code_indexer.server.services.shared_job_sentinel import (
        AnalysisAlreadyRunningError as AnalysisAlreadyRunningError,
        SharedJobSentinel as SharedJobSentinel,
    )
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated as is_content_seam_delegated,
    )
    from code_indexer.server.storage.database_manager import (
        DatabaseConnectionManager as DatabaseConnectionManager,
    )
    from code_indexer.server.storage.filesystem_backends import (
        dashboard_cache_backend_for as dashboard_cache_backend_for,
    )
    from code_indexer.server.storage.json_column import (
        parse_json_column as parse_json_column,
    )
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend as DescriptionRefreshTrackingBackend,
        DependencyMapTrackingBackend as DependencyMapTrackingBackend,
    )
    from code_indexer.server.telemetry.spans import (
        create_span as create_span,
    )
    from code_indexer.server.utils.cluster_node_id import (
        configured_node_id as configured_node_id,
    )
    from code_indexer.server.web.auth import (
        get_session_manager as get_session_manager,
    )
    from code_indexer.server.web.pages._shared import (
        _create_login_redirect as _create_login_redirect,
        _get_golden_repo_manager as _get_golden_repo_manager,
        _require_admin_session as _require_admin_session,
    )
    from code_indexer.utils.file_locking import (
        nfs_safe_fsync as nfs_safe_fsync,
    )
    from code_indexer.utils.subprocess_diagnostics import (
        format_completed_process_diagnostic as format_completed_process_diagnostic,
        redact_url_credentials as redact_url_credentials,
    )

#: name -> (core module, whether the name is a submodule of it). Resolved on
#: first use.
_LAZY_NAMES: Dict[str, Tuple[str, bool]] = {
    "ActivityJournalService": (
        "code_indexer.server.services.activity_journal_service",
        False,
    ),
    "AnalysisAlreadyRunningError": (
        "code_indexer.server.services.shared_job_sentinel",
        False,
    ),
    "AnomalyAggregate": ("code_indexer.server.services.dep_map_parser_hygiene", False),
    "AnomalyEntry": ("code_indexer.server.services.dep_map_parser_hygiene", False),
    "AnomalyType": ("code_indexer.server.services.dep_map_parser_hygiene", False),
    "BackfillJournalService": (
        "code_indexer.server.services.backfill_journal_service",
        False,
    ),
    "CIDX_META_REPO": ("code_indexer.server.services.constants", False),
    "ClaudeInvoker": ("code_indexer.server.services.claude_invoker", False),
    "CliDispatcher": ("code_indexer.server.services.cli_dispatcher", False),
    "CodexInvoker": ("code_indexer.server.services.codex_invoker", False),
    "DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS": (
        "code_indexer.server.services.jittered_dispatcher",
        False,
    ),
    "DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS": (
        "code_indexer.server.services.jittered_dispatcher",
        False,
    ),
    "DEPENDENCY_MAP_DIR": ("code_indexer.server.services.cidx_meta_read_mirror", False),
    "DatabaseConnectionManager": (
        "code_indexer.server.storage.database_manager",
        False,
    ),
    "DepMapSource": ("code_indexer.server.services.dep_map_source", False),
    "DependencyMapTrackingBackend": (
        "code_indexer.server.storage.sqlite_backends",
        False,
    ),
    "DescriptionRefreshTrackingBackend": (
        "code_indexer.server.storage.sqlite_backends",
        False,
    ),
    "catalog_read_path": ("code_indexer.server.services.cidx_meta_read_mirror", False),
    "DuplicateJobError": ("code_indexer.server.services.job_tracker", False),
    "FilesystemDepMapSource": ("code_indexer.server.services.dep_map_source", False),
    "LifecycleBatchRunner": ("code_indexer.global_repos.lifecycle_batch_runner", False),
    "LifecycleFleetScanner": (
        "code_indexer.global_repos.lifecycle_batch_runner",
        False,
    ),
    "SharedJobSentinel": ("code_indexer.server.services.shared_job_sentinel", False),
    "_create_login_redirect": ("code_indexer.server.web.pages._shared", False),
    "_get_golden_repo_manager": ("code_indexer.server.web.pages._shared", False),
    "_mcp_response": ("code_indexer.server.mcp.handlers._utils", False),
    "_require_admin_session": ("code_indexer.server.web.pages._shared", False),
    "aggregate_anomalies": (
        "code_indexer.server.services.dep_map_parser_hygiene",
        False,
    ),
    "app": ("code_indexer.server.app", False),
    "background_job_manager": ("code_indexer.server.app", False),
    "build_codex_mcp_auth_header_provider": (
        "code_indexer.server.services.codex_mcp_auth_header_provider",
        False,
    ),
    "build_cross_domain_connections": (
        "code_indexer.server.services.dep_map_parser_tables",
        False,
    ),
    "build_dep_map_dispatcher": (
        "code_indexer.server.services.dep_map_dispatcher_factory",
        False,
    ),
    "build_name_description": (
        "code_indexer.server.services.dep_map_parser_tables",
        False,
    ),
    "build_participating_repos": (
        "code_indexer.server.services.dep_map_parser_tables",
        False,
    ),
    "check_query_admission": (
        "code_indexer.server.services.query_admission_gate",
        False,
    ),
    "configured_node_id": ("code_indexer.server.utils.cluster_node_id", False),
    "content_seam_source": ("code_indexer.server.services.file_service", False),
    "pod_directory": ("code_indexer.server.services.file_service", False),
    "create_span": ("code_indexer.server.telemetry.spans", False),
    "dashboard_cache_backend_for": (
        "code_indexer.server.storage.filesystem_backends",
        False,
    ),
    "deduplicate_anomalies": (
        "code_indexer.server.services.dep_map_parser_hygiene",
        False,
    ),
    "dependencies": ("code_indexer.server.auth", True),
    "describe_scheduler_lock_holder": (
        "code_indexer.global_repos.write_lock_manager",
        False,
    ),
    "format_completed_process_diagnostic": (
        "code_indexer.utils.subprocess_diagnostics",
        False,
    ),
    "get_config_service": ("code_indexer.server.services.config_service", False),
    "get_domain_md_files": ("code_indexer.server.services.dep_map_file_utils", False),
    "get_prompt": ("code_indexer.global_repos.prompts", False),
    "get_session_manager": ("code_indexer.server.web.auth", False),
    "has_yaml_frontmatter": ("code_indexer.server.services.dep_map_file_utils", False),
    "is_content_seam_delegated": (
        "code_indexer.server.startup.content_backend_wiring",
        False,
    ),
    "is_prose_fragment": ("code_indexer.server.services.dep_map_parser_hygiene", False),
    "iter_table_rows": ("code_indexer.server.services.dep_map_parser_tables", False),
    "job_tracker": ("code_indexer.server.app", False),
    "load_domains_json": ("code_indexer.server.services.dep_map_file_utils", False),
    "manifest_entries": ("code_indexer.server.services.file_service", False),
    "memory_pressure_mcp_payload": (
        "code_indexer.server.services.query_admission_gate",
        False,
    ),
    "nfs_safe_fsync": ("code_indexer.utils.file_locking", False),
    "normalize_identifier": (
        "code_indexer.server.services.dep_map_parser_hygiene",
        False,
    ),
    "parse_frontmatter_strict": (
        "code_indexer.server.services.dep_map_parser_tables",
        False,
    ),
    "parse_incoming_table": (
        "code_indexer.server.services.dep_map_parser_tables",
        False,
    ),
    "parse_json_column": ("code_indexer.server.storage.json_column", False),
    "parse_last_analyzed": (
        "code_indexer.server.services.dep_map_parser_tables",
        False,
    ),
    "parse_roles_table": ("code_indexer.server.services.dep_map_parser_tables", False),
    "parse_simple_yaml": ("code_indexer.server.services.dep_map_file_utils", False),
    "parse_yaml_frontmatter": (
        "code_indexer.server.services.dep_map_file_utils",
        False,
    ),
    "prepare_stable_neutral_cwd": (
        "code_indexer.server.services.agent_cli_isolation",
        False,
    ),
    "read_current_commit": ("code_indexer.server.services.metadata_reader", False),
    "redact_url_credentials": ("code_indexer.utils.subprocess_diagnostics", False),
    "remove_mcp_config_file": (
        "code_indexer.server.services.agent_cli_isolation",
        False,
    ),
    "request_cidx_meta_refresh": (
        "code_indexer.global_repos.meta_description_hook",
        False,
    ),
    "shell_templates": ("code_indexer.server.web.pages._shared", False),
    "sleep_with_jitter": ("code_indexer.server.services.jittered_dispatcher", False),
    "strip_backticks": ("code_indexer.server.services.dep_map_parser_hygiene", False),
    "synced_read_root": ("code_indexer.server.services.cidx_meta_read_mirror", False),
    "try_build_mcp_config_file": (
        "code_indexer.server.services.agent_cli_isolation",
        False,
    ),
    "yaml_quote_if_unsafe": ("code_indexer.global_repos.yaml_emitter_utils", False),
}

#: Names this adapter gives a core name under another name.
_RENAMED: Dict[str, str] = {
    "shell_templates": "templates",
}

#: What :func:`server_platform` returns.
ServerPlatformHost = capabilities.ServerPlatformHost


def server_platform() -> ServerPlatformHost:
    """The server's platform pieces.

    Raises:
        RuntimeError: no server is loaded in this process (the slot is filled
            when the core's server package is imported).
    """
    host = capabilities.SERVER_PLATFORM_HOST.get()
    if host is None:
        importlib.import_module("code_indexer.server")
        host = capabilities.SERVER_PLATFORM_HOST.get()
    if host is None:
        raise RuntimeError(
            "the code-indexer server platform is not loaded in this process"
        )
    return host


def __getattr__(name: str) -> object:
    entry = _LAZY_NAMES.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, is_submodule = entry
    if is_submodule:
        return importlib.import_module(f"{module_name}.{name}")
    if name in _RENAMED:
        return getattr(importlib.import_module(module_name), _RENAMED[name])
    return getattr(importlib.import_module(module_name), name)
