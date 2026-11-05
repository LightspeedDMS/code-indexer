"""The ONE module of ``cidx_scip`` that imports the code-indexer core.

SCIP is meant to import ``cidx_sdk`` and third-party packages only. The
capability and seam interfaces it needs, a few pure core helpers and the
server pieces its MCP handlers, REST routes and build lifecycle use are not
in the SDK yet, so this adapter reaches them in ``code_indexer`` and every
other module of the package imports them from here. When the SDK provides
them, this file switches to the SDK and nothing else changes. The import
contract ``scip-feature-no-core`` (``.importlinter``) counts these imports in
its ratchet baseline; no other module of ``cidx_scip`` may add one.

The distribution does not declare ``code-indexer`` as a dependency (the core
depends on ``cidx-scip``, not the reverse), so until this adapter is gone
``cidx_scip`` runs only where the core is installed, which is everywhere the
core loads it.

What each import is for, and what replaces it:

=============================  ============================================  ===========================
Name                           Used for                                      Replaced by
=============================  ============================================  ===========================
``capabilities``               content seam, read scope, telemetry spans     ``cidx_sdk`` capabilities
                               (install slots the host fills)                through ``FeatureContext``
``index_root``                 where a repository's index lives on this      the index-store seam's
                               pod (honours the installed index store)       ``index_root`` (SDK)
path confinement helpers       containment of SCIP database paths            SDK utilities
``is_immutable_versioned_      the snapshot path rule                        the ``snapshot_predicate``
snapshot``                                                                   capability
``write_json_atomic``          the SCIP database migration marker            an SDK utility
``SandboxMcpClient``           the delegated build's sandbox-mcp client      the ``cidx-delegate-
(lazy)                                                                       sandbox`` distribution
CLI helpers (lazy)             ``cidx scip``: mode detection, the remote     a CLI capability
                               API client base, the disabled-command guard
library helpers (lazy)         alias resolution, manifest pruning, the      SDK utilities / the
                               unborn-HEAD test                              content and git seams
``server_platform()``          what the MCP handlers, REST routes, web page  SDK capabilities as each
                               and build lifecycle need from the server      is added (see
                               beyond the SDK capabilities (the              ``ServerPlatformHost``)
                               ``SERVER_PLATFORM_HOST`` slot, through
                               ``capabilities`` above: no further import)
=============================  ============================================  ===========================

Tests that patch a core binding (``code_indexer.kernel.capabilities.X``) keep
working: ``capabilities`` is the core's module object itself, and every lazy
name is looked up in its core module when first used.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Dict

from cidx_sdk.seams.content import (
    ContentSeamSource as ContentSeamSource,
    NOT_FOUND_FETCH_ERRORS as NOT_FOUND_FETCH_ERRORS,
)
from cidx_sdk.seams.index_store import INDEX_DIRNAME as INDEX_DIRNAME

from code_indexer.config import write_json_atomic as write_json_atomic
from code_indexer.kernel import capabilities as capabilities
from code_indexer.utils.index_store import index_root as index_root
from code_indexer.utils.path_confinement import (
    has_git_segment as has_git_segment,
    is_readable_within_root as is_readable_within_root,
    lexical_relative_path as lexical_relative_path,
    resolve_if_within_root as resolve_if_within_root,
    resolves_into_git_directory as resolves_into_git_directory,
)
from code_indexer.utils.snapshot_paths import (
    is_immutable_versioned_snapshot as is_immutable_versioned_snapshot,
)

if TYPE_CHECKING:
    from code_indexer.api_clients.base_client import (
        APIClientError as APIClientError,
        CIDXRemoteAPIClient as CIDXRemoteAPIClient,
    )
    from code_indexer.disabled_commands import require_mode as require_mode
    from code_indexer.mode_detection.command_mode_detector import (
        CommandModeDetector as CommandModeDetector,
        find_project_root as find_project_root,
    )
    from code_indexer.global_repos.alias_manager import (
        AliasManager as AliasManager,
        resolve_alias_or_index_path as resolve_alias_or_index_path,
    )
    from code_indexer.indexing.repo_manifest import (
        accepts_prune_dirs as accepts_prune_dirs,
    )
    from code_indexer.services.git_head import is_unborn_head as is_unborn_head
    from code_indexer.utils.sandbox_mcp_client import (
        SandboxMcpClient as SandboxMcpClient,
        SandboxMcpError as SandboxMcpError,
    )

#: Names resolved on first use, so importing a light SCIP module never loads
#: the HTTP client stack or the CLI's mode detection.
_LAZY_NAMES: Dict[str, str] = {
    "SandboxMcpClient": "code_indexer.utils.sandbox_mcp_client",
    "SandboxMcpError": "code_indexer.utils.sandbox_mcp_client",
    "CIDXRemoteAPIClient": "code_indexer.api_clients.base_client",
    "APIClientError": "code_indexer.api_clients.base_client",
    "require_mode": "code_indexer.disabled_commands",
    "CommandModeDetector": "code_indexer.mode_detection.command_mode_detector",
    "find_project_root": "code_indexer.mode_detection.command_mode_detector",
    "accepts_prune_dirs": "code_indexer.indexing.repo_manifest",
    "is_unborn_head": "code_indexer.services.git_head",
    "AliasManager": "code_indexer.global_repos.alias_manager",
    "resolve_alias_or_index_path": "code_indexer.global_repos.alias_manager",
}


#: What :func:`server_platform` returns.
ServerPlatformHost = capabilities.ServerPlatformHost


def server_platform() -> ServerPlatformHost:
    """The server's platform pieces (see the table above).

    Raises:
        RuntimeError: no server is loaded in this process (the slot is filled
            when the core's server package is imported), so there is nothing
            to serve an MCP tool, REST route or build with.
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
