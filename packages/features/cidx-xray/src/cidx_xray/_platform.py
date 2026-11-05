"""The ONE module of ``cidx_xray`` that imports the code-indexer core.

X-Ray is meant to import ``cidx_sdk`` and third-party packages only. The
capability and seam Protocols it needs, and a few pure core helpers, are not
in the SDK yet, so this adapter reaches them in ``code_indexer`` and every
other module of the package imports them from here. When the SDK provides
them, this file switches to the SDK and nothing else changes. The import
contract ``xray-feature-no-core`` (``.importlinter``) counts these imports in
its ratchet baseline; no other module of the package may add one.

The distribution does not declare ``code-indexer`` as a dependency (the core
depends on ``cidx-xray``, not the reverse), so until this adapter is gone
``cidx_xray`` runs only where the core is installed, which is everywhere the
core loads it.

What each import is for, and what replaces it:

=============================  ============================================  ===========================
Name                           Used for                                      Replaced by
=============================  ============================================  ===========================
``capabilities``               content seam, regex target, X-Ray placement,  ``cidx_sdk`` capabilities
                               evaluator cache store, runtime config,        through ``FeatureContext``
                               telemetry (install slots the host fills)
seam types                     content seam DTOs, X-Ray execution seam,      ``cidx_sdk.seams`` (done,
                               evaluator cache Protocol, ``INDEX_DIRNAME``   SDK 0.3)
``RegexSearchService``         Phase 1 candidate selection (ripgrep)         a regex-search capability
``PathPatternMatcher``         include/exclude path selection shared with    a path-selection capability
                               the indexed query path                        (or an SDK utility)
path confinement, worker       pure helpers shared with the core             SDK utilities
budget, process-group
termination
``BoundedPattern``             time-budgeted regex for the Phase 1 path      SDK utility (needs the
                               driver (shared with the core's caller         ``regex`` package, which
                               pattern sites)                                the SDK cannot depend on)
``server_platform()``          what the MCP handlers, REST routes and        SDK capabilities as each
                               on_startup need from the server beyond the    is added (see
                               SDK capabilities: job tracker lifecycle,      ``ServerPlatformHost``)
                               paged payload-cache writes, the search-event
                               recorder, the repository guard, repository
                               resolution, query admission, content-seam
                               manifest reads, cidx-meta writes (the
                               ``SERVER_PLATFORM_HOST`` slot, through
                               ``capabilities`` above: no further import)
=============================  ============================================  ===========================

Tests that patch a core binding (``code_indexer.kernel.capabilities.X``,
``code_indexer.global_repos.regex_search.X``) keep working: ``capabilities`` is
the core's module object itself, and every other name is looked up by the
X-Ray module that binds it, where tests patch it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Dict

from cidx_sdk.seams.content import (
    ContentSeamSource as ContentSeamSource,
    ManifestDeadlineExceeded as ManifestDeadlineExceeded,
)
from cidx_sdk.seams.index_store import INDEX_DIRNAME as INDEX_DIRNAME
from cidx_sdk.seams.xray import (
    BatchResult as BatchResult,
    XrayCacheBackend as XrayCacheBackend,
    XrayExecutionBackend as XrayExecutionBackend,
    XrayGraphRequest as XrayGraphRequest,
    XrayScanOutcome as XrayScanOutcome,
    XrayScanRequest as XrayScanRequest,
)

from code_indexer.kernel import capabilities as capabilities
from code_indexer.utils.path_confinement import (
    has_git_segment as has_git_segment,
    is_readable_within_root as is_readable_within_root,
)
from code_indexer.utils.process_group import (
    terminate_process_group as terminate_process_group,
)
from code_indexer.utils.worker_budget import (
    detect_worker_count as detect_worker_count,
    per_process_budget as per_process_budget,
    read_applied_worker_count as read_applied_worker_count,
    read_argv_workers as read_argv_workers,
    read_config_json_workers as read_config_json_workers,
)

if TYPE_CHECKING:
    from code_indexer.global_repos.regex_search import (
        RegexSearchService as RegexSearchService,
        RipgrepExecutionError as RipgrepExecutionError,
    )
    from code_indexer.services.path_pattern_matcher import (
        InvalidPatternError as InvalidPatternError,
        PathPatternMatcher as PathPatternMatcher,
    )
    from code_indexer.utils.bounded_regex import (
        BoundedPattern as BoundedPattern,
        PatternBudgetExhausted as PatternBudgetExhausted,
    )

#: Names resolved on first use, so importing a light X-Ray module (the
#: execution seam, the sandbox) never loads the regex search stack, exactly as
#: before the move: only the search engine needs it, and it binds these at its
#: own import.
_LAZY_NAMES: Dict[str, str] = {
    "RegexSearchService": "code_indexer.global_repos.regex_search",
    "RipgrepExecutionError": "code_indexer.global_repos.regex_search",
    "PathPatternMatcher": "code_indexer.services.path_pattern_matcher",
    "InvalidPatternError": "code_indexer.services.path_pattern_matcher",
    "BoundedPattern": "code_indexer.utils.bounded_regex",
    "PatternBudgetExhausted": "code_indexer.utils.bounded_regex",
}


#: What :func:`server_platform` returns.
ServerPlatformHost = capabilities.ServerPlatformHost


def server_platform() -> ServerPlatformHost:
    """The server's platform pieces (see the table above).

    Raises:
        RuntimeError: no server is loaded in this process (the slot is filled
            when the core's server package is imported), so there is nothing
            to serve an MCP tool or REST route with.
    """
    host = capabilities.SERVER_PLATFORM_HOST.get()
    if host is None:
        raise RuntimeError(
            "the code-indexer server platform is not loaded in this process"
        )
    return host


def __getattr__(name: str) -> object:
    module_name = _LAZY_NAMES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
